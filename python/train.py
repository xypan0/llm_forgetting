from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import transformers
from accelerate import Accelerator, FullyShardedDataParallelPlugin
from data import JsonDataset, tokenize_prompt, tokenize_conversion, tokenize_text_only
from prompt_maker import PromptMaker
import random
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader
import logging
import logging.config
import os
import sys
import wandb
from functools import partial
from parse_args import parse_argument, parse_args_dict
from transformers import get_linear_schedule_with_warmup, get_cosine_schedule_with_warmup, AutoModelForCausalLM, AutoTokenizer
from torch import linalg as LA
from NormModel import ModelWithLPNorm
from utils import get_optimizer, evaluate_and_logging, make_tqdm, save_model, logging_stat_dict
import time
import shutil


def get_dataset(json_data_dir: str, response_loss_only, tokenizer, max_length, sharegpt_format: bool, pretrain: bool):
    if pretrain:
        print('preparing dataset')
        transform=partial(tokenize_text_only, response_loss_only=response_loss_only, max_length=max_length, tokenizer=tokenizer)
    elif sharegpt_format:
        transform=partial(tokenize_conversion, response_loss_only=response_loss_only, max_length=max_length, tokenizer=tokenizer)
    else:
        transform=partial(tokenize_prompt, response_loss_only=response_loss_only, max_length=max_length, tokenizer=tokenizer, prompt_maker=PromptMaker())

    dataset=JsonDataset(json_data=json_data_dir, 
                                 shuffle=True, train=True,
                                 transform=transform,
                                 chunk_long_text=pretrain)
    return dataset



def load_data(
    args,
):
    """Loads data in pytorch.

    Args:
        dataset_name: str. Supported datasets ['cifar10', 'cifar100'].
        batch_size: int.
        train_sample_percentage: float.
        validation_sample_percentage: float.
        args: The parsed commandline arguments.
    Returns:
        (train_loader, val_loader, stat), where,
            * train_loader: a DataLoader object for loading training data.
            * val_loader, a DataLoader object for loading val data.
            * stat_dict: a dict maps data statistics names to their values, e.g.
                'num_sample', 'num_class'.
    """
    # Chooses dataset
    tokenizer=transformers.AutoTokenizer.from_pretrained(args.tokenizer_name)
    if not tokenizer.pad_token:
        tokenizer.pad_token = tokenizer.eos_token
    train_dataset = get_dataset(
        args.train_data,
        args.response_loss_only,
        tokenizer,
        max_length=args.max_length,
        sharegpt_format=args.sharegpt_format,
        pretrain=args.pretrain
    )
    print(train_dataset)
    print(train_dataset[0])
    val_dataset = get_dataset(
        args.val_data,
        args.response_loss_only,
        tokenizer,
        max_length=args.max_length,
        sharegpt_format=args.sharegpt_format,
        pretrain=False
    )


    train_loader = DataLoader(
        train_dataset,
        shuffle=True,
        collate_fn=transformers.DataCollatorForSeq2Seq(tokenizer, padding=True, pad_to_multiple_of=8, return_tensors="pt"),
        num_workers=args.num_dataload_worker,
        batch_size=args.micro_batch_size)


    val_loader = DataLoader(
        val_dataset,
        shuffle=True,
        collate_fn=transformers.DataCollatorForSeq2Seq(tokenizer, padding=True, pad_to_multiple_of=8, return_tensors="pt"),
        num_workers=args.num_dataload_worker,
        batch_size=args.val_batch_size)


    # Prepares necessary training information
    stat_dict = {}

    return train_loader, val_loader, stat_dict

def norm(model, accelerator, lambda_=0.5):
    # print(model.parameters())
    # print(accelerator.process_index)
    wd=2*lambda_
    total_l=0.
    for p in model.parameters():
        # print(p, p.shape[0])
        if p.shape[0]:
            res=lambda_*(LA.vector_norm(p)**2)
            # print(res)
            # res.backward()
            # accelerator.backward(res)
            total_l+=res
            # p.grad.add_(wd*p)
    # print(optimizer.state_dict())
    return total_l

def norm_backward(model, accelerator, lambda_=0.5):
    su=0
    for p in model.parameters():
        if p.shape[0]:
            res=lambda_*(LA.vector_norm(p)**2)
            su+=res
            # accelerator.backward(res)
    # accelerator.backward(su)
    if accelerator.is_main_process:
        print(su)
        print('bkw')

def norm_add(model, accelerator, lambda_=0.5):
    wd=2*lambda_
    for p in model.parameters():
        if p.shape[0]:
            p.grad.add_(wd*p)

def optimize(
        train_loader,
        val_loader,
        args,
        optimizer_args_dict,
        accelerator
    ):

    grad_accumulation_steps = args.global_batch_size // args.micro_batch_size
    world_size = int(os.environ.get("WORLD_SIZE", 1))
    grad_accumulation_steps = grad_accumulation_steps // world_size
    if args.max_steps is None:
        max_steps = len(train_loader.dataset) // args.global_batch_size * args.epoch
    else:
        max_steps = args.max_steps
    accelerator.print(f'max_steps: {max_steps}, grad_accu_steps: {grad_accumulation_steps}')

    if args.bf16:
        model = AutoModelForCausalLM.from_pretrained(args.model, attn_implementation="flash_attention_2", torch_dtype=torch.bfloat16)
    else:
        model = AutoModelForCausalLM.from_pretrained(args.model, torch_dtype=torch.float32)

    if args.norm is not None:
        if args.diff_norm:
            print('using diff_norm')
            if args.bf16:
                base_model = AutoModelForCausalLM.from_pretrained(args.model, attn_implementation="flash_attention_2", torch_dtype=torch.bfloat16)
            else:
                base_model = AutoModelForCausalLM.from_pretrained(args.model, torch_dtype=torch.float32)
            model=ModelWithLPNorm(targetModule=model, baseModule=base_model, lambda_for_norm=args.norm)
        else:
            model=ModelWithLPNorm(targetModule=model, lambda_for_norm=args.norm)

    print(optimizer_args_dict)
    optimizer = get_optimizer(model.parameters(), optimizer_args_dict)
    
    warmup_steps = int(args.warmup_ratio * max_steps)
    lr_scheduler = get_cosine_schedule_with_warmup(
        optimizer=optimizer,
        num_warmup_steps=warmup_steps,
        num_training_steps=max_steps,
    )
    train_iterator = iter(train_loader)

    model, optimizer, train_loader, val_loader = accelerator.prepare(model, optimizer, train_loader, val_loader)
    
    accelerator.print(model)
    
    # for step in range(max_steps):
    start_time=time.time()

    for step in make_tqdm(accelerator, list(range(max_steps))):
        lr_scheduler.step()

        total_loss = 0.
        total_norm = 0.
        # with accelerator.accumulate(model):
        evaluate_and_logging(
            model=model,
            global_step=step,
            accelerator=accelerator,
            start_time=start_time,
            val_loader=val_loader,
            args=args
        )

        model.train()

        for inner_step in range(grad_accumulation_steps):
            try:
                batch = next(train_iterator)
            except Exception as e:
                train_iterator = iter(train_loader)
                batch = next(train_iterator)

            # print(batch)
            batch.to(accelerator.device)
            x_batch=batch['input_ids']
            y_batch=batch['labels']
            attn_mask=batch['attention_mask']

            if args.norm is not None:
                act_loss, sum_norm, outputs = model(x_batch, labels=y_batch, attention_mask=attn_mask)
                
            else: 
                outputs = model(x_batch, labels=y_batch, attention_mask=attn_mask)
                act_loss=outputs.loss.clone()
                sum_norm=torch.tensor([0])

            loss = outputs.loss / grad_accumulation_steps
            act_loss = act_loss / grad_accumulation_steps
            total_loss += accelerator.gather(act_loss).detach().cpu().mean()
            total_norm += accelerator.gather(sum_norm).detach().cpu().mean()

            accelerator.backward(loss, retain_graph=True)

        optimizer.step()
        optimizer.zero_grad()
        stat_dict = {
            'train loss': total_loss,
            'train norm': total_norm,
            'step': step,
            'time': time.time() - start_time,
            'lr': lr_scheduler.get_lr()
        }
        logging_stat_dict(
            stat_dict,
            prefix=f'At the beginning of i = {step},',
            suffix='',
            use_wandb=args.use_wandb,
            accelerator=accelerator
        )

    if accelerator.is_main_process and args.save_dir is not None and os.path.exists(args.save_dir):
        accelerator.print(f"delete model at {args.save_dir} ...")
        shutil.rmtree(args.save_dir)
        
    if args.save_dir is not None:
        tokenizer=AutoTokenizer.from_pretrained(args.tokenizer_name)
        save_model(accelerator, model.targetModule, tokenizer, args.save_dir)


def main():
    """Uses deep learning models to analyze SGD with learning rate schedule."""
    # Parses arguments and loads configurations
    args = parse_argument(sys.argv)
    optimizer_args_dict = parse_args_dict(args.optimizer)
    # lr_scheduler_args_dict = parse_args_dict(args.lr_scheduler)
    logging.config.fileConfig(args.logging_conf_file)
    logging.info('#################################################')
    logging.info('args = %s', str(args))

    # Controls pseudorandom behavior
    if args.pseudo_random:
        os.environ['PYTHONHASHSEED'] = '0'
        os.environ['TF_DETERMINISTIC_OPS'] = '1'
        random.seed(args.seed + 1)
        np.random.seed(args.seed + 1)
        torch.manual_seed(args.seed)
        torch.cuda.manual_seed(args.seed)
        torch.cuda.manual_seed_all(args.seed)
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False
        # torch.use_deterministic_algorithms(True)
        print(f'set seed to {args.seed}')

    # Loads training/validation/test data
    train_loader, val_loader, stat_dict = load_data(args=args)
    
    fsdp_plugin = FullyShardedDataParallelPlugin()
    fsdp_plugin.set_mixed_precision("fp32")
    
    accelerator=Accelerator(mixed_precision='bf16',
                                gradient_accumulation_steps=1) if args.bf16 else Accelerator(gradient_accumulation_steps=1, fsdp_plugin=fsdp_plugin)
    print(accelerator.state.fsdp_plugin)

    # Runs optimization
    if args.use_wandb and accelerator.is_main_process:
        wandb.init(
            project=args.wandb_project,
            name=args.wandb_run_name,
            config=args
        )
    try:
        optimize(
            train_loader=train_loader,
            val_loader=val_loader,
            args=args,
            optimizer_args_dict=optimizer_args_dict,
            accelerator=accelerator
        )
    except Exception as e:
        if args.use_wandb and accelerator.is_main_process:
            wandb.finish()
        raise e

    if args.use_wandb and accelerator.is_main_process:
        wandb.finish()


if __name__ == '__main__':
    main()
# ------------------------------------------------------------------------
# DETR
# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
# ------------------------------------------------------------------------
# Additionally modified by NAVER Corp. for ViDT
# ------------------------------------------------------------------------

import sys
sys.path.append('vidt')     # for colab
sys.path.append('/kaggle/working/vidt') # for kaggle
sys.path.append('/kaggle/working')
import os
import datetime
import json
import random
import time
from pathlib import Path

import numpy as np
import torch
from torch.utils.data import DataLoader, DistributedSampler
import resource
import vidt.datasets
import util.misc as utils
from vidt.datasets import build_dataset, get_coco_api_from_dataset
from engine import evaluate, train_one_epoch, train_one_epoch_with_teacher
from methods import build_model
from util.scheduler import create_scheduler
from arguments import get_args_parser
import argparse


def build_distil_model(args):
    """ build a teacher model """
    assert args.distil_model in ['vidt_nano', 'vidt_tiny', 'vidt_small', 'vidt_base']
    return build_model(args, is_teacher=True)

def main(args):
    """ main function to train a ViDT model """

    rlimit = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (4096, rlimit[1]))

    # Gradient accumulation setup
    if args.n_iter_to_acc > 1:
        if args.batch_size % args.n_iter_to_acc != 0:
            print("Not supported divisor for acc grade.")
            import sys
            sys.exit(1)
        print("Gradient Accumulation is applied.")
        print("The batch: ", args.batch_size, "->", int(args.batch_size / args.n_iter_to_acc),
              'but updated every ', args.n_iter_to_acc, 'steps.')
        args.batch_size = args.batch_size // args.n_iter_to_acc
    ##
    args.gpu = 0
    utils.init_distributed_mode(args)
    print("git:\n  {}\n".format(utils.get_sha()))
    print(args)
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print('device location:', device)
    # fix the seed for reproducibility
    seed = args.seed
    torch.manual_seed(seed)
    np.random.seed(seed)
    random.seed(seed)

    # import pdb;pdb.set_trace()
    model, criterion, postprocessors = build_model(args)
    model.to(device)

    # parallel model setup
    model_without_ddp = model
    if args.distributed:
        model = torch.nn.parallel.DistributedDataParallel(model,
                                                          device_ids=[args.gpu],
                                                          find_unused_parameters=True)
        model_without_ddp = model.module
    # print parameter info.
    n_parameters = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print('number of params:', n_parameters)
    # optimizer setup
    def build_optimizer(model, args):
        if hasattr(model.backbone, 'no_weight_decay'):
            skip = model.backbone.no_weight_decay()
        head = []
        backbone_decay = []
        backbone_no_decay = []
        for name, param in model.named_parameters():
            if "backbone" not in name and param.requires_grad:
                head.append(param)
            if "backbone" in name and param.requires_grad:
                if len(param.shape) == 1 or name.endswith(".bias") or name.split('.')[-1] in skip:
                    backbone_no_decay.append(param)
                else:
                    backbone_decay.append(param)
        param_dicts = [
            {"params": head},
            {"params": backbone_no_decay, "weight_decay": 0., "lr": args.lr},
            {"params": backbone_decay, "lr": args.lr},
        ]

        # print the total number of trainable params.
        n_parameters = sum(p.numel() for p in model.parameters() if p.requires_grad)
        print('num of total trainable prams:' + str(n_parameters))

        optimizer = torch.optim.AdamW(param_dicts, lr=args.lr, weight_decay=args.weight_decay)
        return optimizer

    # build an optiimzer along with a learning scheduler
    optimizer = build_optimizer(model_without_ddp, args)
    lr_scheduler, _ = create_scheduler(args, optimizer)

    # build data loader
    dataset_train = build_dataset(image_set='train', args=args)
    dataset_val = build_dataset(image_set='val', args=args)
    print("# train:", len(dataset_train), ", # val", len(dataset_val))

    # data samplers
    if args.distributed:
        sampler_train = DistributedSampler(dataset_train)
        sampler_val = DistributedSampler(dataset_val, shuffle=False)
    else:
        sampler_train = torch.utils.data.RandomSampler(dataset_train)
        sampler_val = torch.utils.data.SequentialSampler(dataset_val)

    batch_sampler_train = torch.utils.data.BatchSampler(
        sampler_train, args.batch_size, drop_last=True)

    data_loader_train = DataLoader(dataset_train, batch_sampler=batch_sampler_train,
                                   collate_fn=utils.collate_fn, num_workers=args.num_workers)
    data_loader_val = DataLoader(dataset_val, args.batch_size, sampler=sampler_val,
                                 drop_last=False, collate_fn=utils.collate_fn, num_workers=args.num_workers)

    if args.dataset_file == "coco_panoptic":
        # We also evaluate AP during panoptic training, on original coco DS
        coco_val = datasets.coco.build("val", args)
        base_ds = get_coco_api_from_dataset(coco_val)
    else:
        base_ds = get_coco_api_from_dataset(dataset_val)

    output_dir = Path(args.output_dir)

    # resume from a checkpoint or eval with a checkpoint
    if args.resume:
        if args.resume.startswith('https'):
            checkpoint = torch.hub.load_state_dict_from_url(
                args.resume, map_location='cpu', check_hash=True)
        else:
            checkpoint = torch.load(args.resume, map_location='cpu')
        model_without_ddp.load_state_dict(checkpoint['model'], strict=False)
        print('load a checkpoint from', args.resume)
        if not args.eval and 'optimizer' in checkpoint and 'lr_scheduler' in checkpoint and 'epoch' in checkpoint:
            optimizer.load_state_dict(checkpoint['optimizer'])
            lr_scheduler.load_state_dict(checkpoint['lr_scheduler'])
            args.start_epoch = checkpoint['epoch'] + 1

    # only evaluation purpose
    if args.eval:
        test_stats, coco_evaluator = evaluate(model, criterion, postprocessors,
                                              data_loader_val, base_ds, device)

        if args.output_dir:
            utils.save_on_master(coco_evaluator.coco_eval["bbox"].eval, output_dir / "eval.pth")
        return

    print("Start training")
    start_time = time.time()
    
    model.eval()
    criterion.eval()
    metric_logger = utils.MetricLogger(delimiter=", ")
    header = 'Test'
    for samples, targets in metric_logger.log_every(data_loader_val, 256, header):
        samples = samples.to(device)
        targets = [{k: v.to(device) for k, v in t.items()} for t in targets]

        # inference
        outputs = model(samples)
        for k in outputs.keys():
            if type(outputs[k]) == torch.Tensor:
                outputs[k] = np.array(outputs[k].cpu().detach()).tolist()
        with open('output.json', 'w') as f:
            json.dump(outputs, f)
        with open('input.json', 'w') as f:
            json.dump(samples, f)
        with open('target.json', 'w') as f:
            json.dump(targets, f)
        
        break
    
#     for epoch in range(args.start_epoch, args.epochs):
#         if args.distributed:
#             sampler_train.set_epoch(epoch)
#         # training one epoch
#         train_stats = train_one_epoch(
#             model, criterion, data_loader_train, optimizer, device, epoch,
#             args.clip_max_norm, n_iter_to_acc=args.n_iter_to_acc, print_freq=args.print_freq)
#         lr_scheduler.step(epoch)

#         # model save
#         if args.output_dir:
#             checkpoint_paths = [output_dir / 'checkpoint.pth']
#             # extra checkpoint before LR drop and every 100 epochs
#             if (epoch + 1) % args.lr_drop == 0 or (epoch + 1) % 100 == 0:
#                 checkpoint_paths.append(output_dir / f'checkpoint{epoch:04}.pth')
#             for checkpoint_path in checkpoint_paths:
#                 utils.save_on_master({
#                     'model': model_without_ddp.state_dict(),
#                     'optimizer': optimizer.state_dict(),
#                     'lr_scheduler': lr_scheduler.state_dict(),
#                     'epoch': epoch,
#                     'args': args,
#                 }, checkpoint_path)

#         # evaluation on COCO val.
#         test_stats, coco_evaluator = evaluate(
#             model, criterion, postprocessors, data_loader_val, base_ds, device)

#         # logs
#         log_stats = {**{f'train_{k}': v for k, v in train_stats.items()},
#                      **{f'test_{k}': v for k, v in test_stats.items()},
#                      'epoch': epoch,
#                      'n_parameters': n_parameters}
#         if args.output_dir and utils.is_main_process():
#             with (output_dir / "log.txt").open("a") as f:
#                 f.write(json.dumps(log_stats) + "\n")

#             # for evaluation logs
#             if coco_evaluator is not None:
#                 (output_dir / 'eval').mkdir(exist_ok=True)
#                 if "bbox" in coco_evaluator.coco_eval:
#                     filenames = ['latest.pth']
#                     if epoch % 50 == 0:
#                         filenames.append(f'{epoch:03}.pth')
#                     for name in filenames:
#                         torch.save(coco_evaluator.coco_eval["bbox"].eval,
#                                    output_dir / "eval" / name)

    total_time = time.time() - start_time
    total_time_str = str(datetime.timedelta(seconds=int(total_time)))
    print('Training time {}'.format(total_time_str))


if __name__ == '__main__':

    parser = argparse.ArgumentParser('ViDT training and evaluation script', parents=[get_args_parser()])
    args = parser.parse_args()

    ''' for testing
    args.method = 'vidt'
    args.backbone_name = 'swin_tiny'
    args.batch_size = 2
    args.num_workers = 2
    args.aux_loss = True
    args.with_box_refine = True
    args.output_dir = 'testing'
    '''

    # set dim_feedforward differently
    # standard Transformers use 2048, while Deformable Transformers use 1024
    if args.method == 'vidt_wo_neck':
        args.dim_feedforward = 2048
    else:
        args.dim_feedforward = 1024

    # log file name
    if args.output_dir == '':
        # default out_dir name if not specified
        args.output_dir += args.method + '-'
        args.output_dir += args.backbone_name + '-'
        args.output_dir += args.sched + '-'
        args.output_dir += str(args.epochs) + '-'
        args.output_dir += str(args.batch_size)
        args.output_dir = args.method + '-' + args.backbone_name.upper() + '-batch-' + \
                          str(args.batch_size) + '-epoch-' + str(args.epochs)

    # make log directories
    if args.output_dir:
        log_main = 'logs'
        args.output_dir = os.path.join(log_main, args.output_dir)
        Path(args.output_dir).mkdir(parents=True, exist_ok=True)
        print('log', args.output_dir)

    main(args)


import datetime
import os
import time
import math
import csv
from collections import OrderedDict

import torch
import torch.utils.data
from torch import nn
import torchvision
from torchvision import transforms

import utils

try:
    from apex import amp
except ImportError:
    amp = None


def update_summary(epoch, train_metrics, eval_metrics, filename, write_header=False):
    rowd = OrderedDict(epoch=epoch)
    rowd.update([('train_' + k, v) for k, v in train_metrics.items()])
    rowd.update([('eval_' + k, v) for k, v in eval_metrics.items()])
    with open(filename, mode='a') as cf:
        dw = csv.DictWriter(cf, fieldnames=rowd.keys())
        if write_header:  # first iteration (epoch == 1 can't be used)
            dw.writeheader()
        dw.writerow(rowd)

def train_one_epoch(model, criterion, optimizer, data_loader, device, epoch, print_freq, apex=False, dgPruner=None, output_dir = ''):
    model.train()
    metric_logger = utils.MetricLogger(delimiter="  ")
    metric_logger.add_meter('lr', utils.SmoothedValue(window_size=1, fmt='{value}'))
    metric_logger.add_meter('img/s', utils.SmoothedValue(window_size=10, fmt='{value}'))

    header = 'Epoch: [{}]'.format(epoch)
    batch_idx = 0
    for image, target in metric_logger.log_every(data_loader, print_freq, header):
        start_time = time.time()
        image, target = image.to(device), target.to(device)
        output = model(image)
        loss = criterion(output, target)

        optimizer.zero_grad()
        if apex:
            with amp.scale_loss(loss, optimizer) as scaled_loss:
                scaled_loss.backward()
        else:
            loss.backward()
        optimizer.step()

        acc1, acc5 = utils.accuracy(output, target, topk=(1, 5))
        batch_size = image.shape[0]
        metric_logger.update(loss=loss.item(), lr=optimizer.param_groups[0]["lr"])
        metric_logger.meters['acc1'].update(acc1.item(), n=batch_size)
        metric_logger.meters['acc5'].update(acc5.item(), n=batch_size)
        metric_logger.meters['img/s'].update(batch_size / (time.time() - start_time))

        # Mehrdad
        # if ( args.prune and ( (batch_idx % dgPruner.num_iter_per_update( len(data_loader) ) ) == 0) ):
        #     dgPruner.dump_growth_stat(output_dir, epoch)
        #     dgPruner.prune_n_reset( epoch + batch_idx / len(data_loader) )
        #     dgPruner.dump_sparsity_stat(model, output_dir, epoch)
        # if (args.prune):
        #     dgPruner.apply_mask_to_weight()
        batch_idx = batch_idx + 1
        # 
    metrics = OrderedDict([('loss', metric_logger.loss), ('top1', metric_logger.acc1), ('top5', metric_logger.acc5)])
    return metrics


def evaluate(model, criterion, data_loader, device, print_freq=100, dgPruner=None, output_dir = ''):
    model.eval()
    metric_logger = utils.MetricLogger(delimiter="  ")
    header = 'Test:'
    with torch.no_grad():
        if (dgPruner):
            dgPruner.dump_growth_stat(output_dir, 1000)
            dgPruner.dump_sparsity_stat(model, output_dir, 1000)

        for image, target in metric_logger.log_every(data_loader, print_freq, header):
            image = image.to(device)
            target = target.to(device)
            output = model(image)
            loss = criterion(output, target)

            acc1, acc5 = utils.accuracy(output, target, topk=(1, 5))
            # FIXME need to take into account that the datasets
            # could have been padded in distributed setup
            batch_size = image.shape[0]
            metric_logger.update(loss=loss.item())
            metric_logger.meters['acc1'].update(acc1.item(), n=batch_size)
            metric_logger.meters['acc5'].update(acc5.item(), n=batch_size)
    # gather the stats from all processes
    metric_logger.synchronize_between_processes()

    print(' * Acc@1 {top1.global_avg:.3f} Acc@5 {top5.global_avg:.3f}'
          .format(top1=metric_logger.acc1, top5=metric_logger.acc5))

    metrics = OrderedDict([('loss', metric_logger.loss), ('top1', metric_logger.acc1), ('top5', metric_logger.acc5)])

    return metrics


def _get_cache_path(filepath):
    import hashlib
    h = hashlib.sha1(filepath.encode()).hexdigest()
    cache_path = os.path.join("~", ".torch", "vision", "datasets", "imagefolder", h[:10] + ".pt")
    cache_path = os.path.expanduser(cache_path)
    return cache_path


def load_data(traindir, valdir, cache_dataset, distributed):
    # Data loading code
    print("Loading data")
    normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                     std=[0.229, 0.224, 0.225])

    print("Loading training data")
    st = time.time()
    cache_path = _get_cache_path(traindir)
    if cache_dataset and os.path.exists(cache_path):
        # Attention, as the transforms are also cached!
        print("Loading dataset_train from {}".format(cache_path))
        dataset, _ = torch.load(cache_path)
    else:
        dataset = torchvision.datasets.ImageFolder(
            traindir,
            transforms.Compose([
                transforms.RandomResizedCrop(224),
                transforms.RandomHorizontalFlip(),
                transforms.ToTensor(),
                normalize,
            ]))
        if cache_dataset:
            print("Saving dataset_train to {}".format(cache_path))
            utils.mkdir(os.path.dirname(cache_path))
            utils.save_on_master((dataset, traindir), cache_path)
    print("Took", time.time() - st)

    print("Loading validation data")
    cache_path = _get_cache_path(valdir)
    if cache_dataset and os.path.exists(cache_path):
        # Attention, as the transforms are also cached!
        print("Loading dataset_test from {}".format(cache_path))
        dataset_test, _ = torch.load(cache_path)
    else:
        dataset_test = torchvision.datasets.ImageFolder(
            valdir,
            transforms.Compose([
                transforms.Resize(256),
                transforms.CenterCrop(224),
                transforms.ToTensor(),
                normalize,
            ]))
        if cache_dataset:
            print("Saving dataset_test to {}".format(cache_path))
            utils.mkdir(os.path.dirname(cache_path))
            utils.save_on_master((dataset_test, valdir), cache_path)

    print("Creating data loaders")
    if distributed:
        train_sampler = torch.utils.data.distributed.DistributedSampler(dataset)
        test_sampler = torch.utils.data.distributed.DistributedSampler(dataset_test)
    else:
        train_sampler = torch.utils.data.RandomSampler(dataset)
        test_sampler = torch.utils.data.SequentialSampler(dataset_test)

    return dataset, dataset_test, train_sampler, test_sampler


def main(args):
    if args.apex and amp is None:
        raise RuntimeError("Failed to import apex. Please install apex from https://www.github.com/nvidia/apex "
                           "to enable mixed-precision training.")

    if args.output_dir:
        utils.mkdir(args.output_dir)

    utils.init_distributed_mode(args)
    print(args)

    device = torch.device(args.device)

    torch.backends.cudnn.benchmark = True

    train_dir = os.path.join(args.data_path, 'train')
    val_dir = os.path.join(args.data_path, 'val')
    dataset, dataset_test, train_sampler, test_sampler = load_data(train_dir, val_dir,
                                                                   args.cache_dataset, args.distributed)
    dataset.samples = [dataset.samples[idx] for idx in range(1024)]
    dataset.targets = [dataset.targets[idx] for idx in range(1024)]
    dataset_test.samples = [dataset.samples[idx] for idx in range(1024)]
    dataset_test.targets = [dataset.targets[idx] for idx in range(1024)]
                                                                   
    data_loader = torch.utils.data.DataLoader(
        dataset, batch_size=args.batch_size,
        sampler=train_sampler, num_workers=args.workers, pin_memory=True)

    data_loader_test = torch.utils.data.DataLoader(
        dataset_test, batch_size=args.batch_size,
        sampler=test_sampler, num_workers=args.workers, pin_memory=True)

    dataset.samples = [dataset.samples[idx] for idx in range(1000)]
    dataset.targets = [dataset.targets[idx] for idx in range(1000)]
    dataset_test.samples = [dataset.samples[idx] for idx in range(100)]
    dataset_test.targets = [dataset.targets[idx] for idx in range(100)]

    print("Creating model")
    model = torchvision.models.__dict__[args.model](pretrained=args.pretrained)
    model.to(device)

    # Mehrdad: Fuse
    # from DG_Prune.FuseHook import FuseHook, modified_fuse_known_modules
    # from torch.quantization.fuse_modules import fuse_modules
    # model.eval()
    # h = FuseHook(model)
    # for image, target in data_loader_test:
    #     output_test = model(image[0].unsqueeze(0))
    #     break
    # model_fused = fuse_modules(model, h.modules_to_fuse, inplace=False, fuser_func=modified_fuse_known_modules )
    # Mehrdad: Prune
    from DG_Prune import DG_Pruner, TaylorImportance, MagnitudeImportance, RigLImportance
    dgPruner = None
    if args.prune:
        dgPruner = DG_Pruner()
        model = dgPruner.swap_prunable_modules(model)
        dgPruner.dump_sparsity_stat(model, args.output_dir, 0)
        dgPruner.sense_analyzers_from_file('DG_Prune/sense_resnet50.json')
        dgPruner.add_custom_pruning(model, MagnitudeImportance)
    #

    if args.distributed and args.sync_bn:
        model = torch.nn.SyncBatchNorm.convert_sync_batchnorm(model)

    criterion = nn.CrossEntropyLoss()

    optimizer = torch.optim.SGD(
        model.parameters(), lr=args.lr, momentum=args.momentum, weight_decay=args.weight_decay)

    if args.apex:
        model, optimizer = amp.initialize(model, optimizer,
                                          opt_level=args.apex_opt_level
                                          )

    # lr_scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=args.lr_step_size, gamma=args.lr_gamma)
    lf = lambda x: ((1 + math.cos(x * math.pi / args.epochs)) / 2) * (1 - args.lrf) + args.lrf  # cosine
    lr_scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, lr_lambda=lf)

    model_without_ddp = model
    if args.distributed:
        model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.gpu])
        model_without_ddp = model.module

    if args.resume:
        checkpoint = torch.load(args.resume, map_location='cpu')
        model_without_ddp.load_state_dict(checkpoint['model'])
        optimizer.load_state_dict(checkpoint['optimizer'])
        lr_scheduler.load_state_dict(checkpoint['lr_scheduler'])
        args.start_epoch = checkpoint['epoch'] + 1

    if args.test_only:
        evaluate(model, criterion, data_loader_test, device=device, print_freq=args.print_freq, dgPruner=dgPruner, output_dir=args.output_dir)
        return

    print("Start training")
    start_time = time.time()
    for lth_stage in range(0, dgPruner.num_stages() + 1):
        if (lth_stage != 0):
            checkpoint = dgPruner.rewind_masked_checkpoint()
            model_without_ddp.load_state_dict(checkpoint['model'])
            optimizer.load_state_dict(checkpoint['optimizer'])
            lr_scheduler.load_state_dict(checkpoint['lr_scheduler'])
            args.start_epoch = checkpoint['epoch'] + 1
            dgPruner.dump_sparsity_stat(model, args.output_dir, lth_stage * 10000)

        for epoch in range(args.start_epoch, args.epochs):
            if args.distributed:
                train_sampler.set_epoch(epoch)
            train_metrics = train_one_epoch(model, criterion, optimizer, data_loader, device, epoch, args.print_freq, args.apex, dgPruner=dgPruner, output_dir=args.output_dir)
            lr_scheduler.step()
            eval_metrics = evaluate(model, criterion, data_loader_test, device=device, print_freq=args.print_freq, dgPruner=dgPruner, output_dir=args.output_dir)

            if args.output_dir:
                checkpoint = {
                    'model': model_without_ddp.state_dict(),
                    'optimizer': optimizer.state_dict(),
                    'lr_scheduler': lr_scheduler.state_dict(),
                    'epoch': epoch,
                    'args': args}
                utils.save_on_master(
                    checkpoint,
                    os.path.join(args.output_dir, 'model_{}.pth'.format(epoch)))
                utils.save_on_master(
                    checkpoint,
                    os.path.join(args.output_dir, 'checkpoint.pth'))
            # Mehrdad: LTH, pruning in the end
            if (args.prune):
                
                if (epoch == args.epochs - 1):
                    dgPruner.prune_n_reset( epoch )
                    dgPruner.dump_sparsity_stat(model, args.output_dir, epoch)
                    dgPruner.apply_mask_to_weight()
                checkpoint = {
                    'model': model_without_ddp.state_dict(),
                    'optimizer': optimizer.state_dict(),
                    'lr_scheduler': lr_scheduler.state_dict(),
                    'epoch': epoch,
                    'args': args} 
                    
                # Save checkpoints
                if (lth_stage == 0) and (epoch == dgPruner.rewind_epoch(args.epochs)):
                    dgPruner.save_rewind_checkpoint(checkpoint)
                if (epoch == args.epochs - 1):
                    dgPruner.save_final_checkpoint(checkpoint)

            update_summary(
                epoch, train_metrics, eval_metrics, os.path.join(args.output_dir, 'summary.csv') )

    total_time = time.time() - start_time
    total_time_str = str(datetime.timedelta(seconds=int(total_time)))
    print('Training time {}'.format(total_time_str))


def parse_args():
    import argparse
    parser = argparse.ArgumentParser(description='PyTorch Classification Training')

    parser.add_argument('--data-path', default='/datasets01/imagenet_full_size/061417/', help='dataset')
    parser.add_argument('--model', default='resnet18', help='model')
    parser.add_argument('--device', default='cuda', help='device')
    parser.add_argument('-b', '--batch-size', default=32, type=int)
    parser.add_argument('--epochs', default=90, type=int, metavar='N',
                        help='number of total epochs to run')
    parser.add_argument('-j', '--workers', default=16, type=int, metavar='N',
                        help='number of data loading workers (default: 16)')
    parser.add_argument('--lr', default=0.1, type=float, help='initial learning rate')
    parser.add_argument('--lrf', default=5e-5, type=float, help='final learning rate')

    parser.add_argument('--momentum', default=0.9, type=float, metavar='M',
                        help='momentum')
    parser.add_argument('--wd', '--weight-decay', default=1e-4, type=float,
                        metavar='W', help='weight decay (default: 1e-4)',
                        dest='weight_decay')
    parser.add_argument('--lr-step-size', default=30, type=int, help='decrease lr every step-size epochs')
    parser.add_argument('--lr-gamma', default=0.1, type=float, help='decrease lr by a factor of lr-gamma')
    parser.add_argument('--print-freq', default=10, type=int, help='print frequency')
    parser.add_argument('--output-dir', default='.', help='path where to save')
    parser.add_argument('--resume', default='', help='resume from checkpoint')
    parser.add_argument('--start-epoch', default=0, type=int, metavar='N',
                        help='start epoch')
    parser.add_argument('--prune', action='store_true', help='use pruning')
    parser.add_argument(
        "--cache-dataset",
        dest="cache_dataset",
        help="Cache the datasets for quicker initialization. It also serializes the transforms",
        action="store_true",
    )
    parser.add_argument(
        "--sync-bn",
        dest="sync_bn",
        help="Use sync batch norm",
        action="store_true",
    )
    parser.add_argument(
        "--test-only",
        dest="test_only",
        help="Only test the model",
        action="store_true",
    )
    parser.add_argument(
        "--pretrained",
        dest="pretrained",
        help="Use pre-trained models from the modelzoo",
        action="store_true",
    )

    # Mixed precision training parameters
    parser.add_argument('--apex', action='store_true',
                        help='Use apex for mixed precision training')
    parser.add_argument('--apex-opt-level', default='O1', type=str,
                        help='For apex mixed precision training'
                             'O0 for FP32 training, O1 for mixed precision training.'
                             'For further detail, see https://github.com/NVIDIA/apex/tree/master/examples/imagenet'
                        )

    # distributed training parameters
    parser.add_argument('--world-size', default=1, type=int,
                        help='number of distributed processes')
    parser.add_argument('--dist-url', default='env://', help='url used to set up distributed training')

    args = parser.parse_args()

    return args


if __name__ == "__main__":
    args = parse_args()
    main(args)

# python built-in library
import os
import argparse
import time
from multiprocessing import Manager
# 3rd party library
import numpy as np
import torch
import torch.nn as nn
from torch.autograd import Variable
from torch.utils.data import DataLoader
from torch.utils.data.sampler import SubsetRandomSampler
from tensorboardX import SummaryWriter
# own code
import config
from model import Model
from dataset import KaggleDataset, Compose
from helper import AverageMeter, iou_mean, save_ckpt, load_ckpt


def main(args):
    model = Model()
    if config.cuda:
        model = model.cuda()
        #model = torch.nn.DataParallel(model).cuda()

    cost = nn.BCELoss()
    optimizer = torch.optim.Adam(model.parameters(), lr=args.learn_rate, weight_decay=1e-6)

    # dataloader workers are forked process thus we need a IPC manager to keep cache in same memory space
    manager = Manager()
    cache = manager.dict()
    # prepare dataset and loader
    dataset = KaggleDataset('data/stage1_train', transform=Compose(), cache=cache)
    train_idx, valid_idx = dataset.split()
    train_loader = DataLoader(
        dataset, sampler=SubsetRandomSampler(train_idx),
        batch_size=config.n_batch,
        num_workers=config.n_worker,
        pin_memory=config.cuda)
    valid_loader = DataLoader(
        dataset, sampler=SubsetRandomSampler(valid_idx),
        batch_size=config.n_batch,
        num_workers=config.n_worker)

    # resume checkpoint
    start_epoch = 0
    if args.resume:
        start_epoch = load_ckpt(model, optimizer)
    if start_epoch == 0:
        print('Grand new training ...')

    # decide log directory name
    log_dir = os.path.join(
        'logs', config.model_name, '{}'.format(config.width),
        'ep_{},{}-lr_{}'.format(
            start_epoch,
            args.epoch + start_epoch,
            args.learn_rate,
        )
    )

    with SummaryWriter(log_dir) as writer:
        if start_epoch == 0 and False:
            # dump graph only for very first training, disable by default
            dump_graph(model, writer) 
        print('Training started...')
        for epoch in range(start_epoch, args.epoch + start_epoch):
            train(train_loader, model, cost, optimizer, epoch, writer)
            if epoch % 3 == 2:
                valid(valid_loader, model, cost, epoch, writer, len(train_loader))
            # save checkpoint per n epoch
            if epoch % config.n_ckpt_epoch == config.n_ckpt_epoch - 1:
                save_ckpt(model, optimizer, epoch+1)
        print('Training finished...')

def dump_graph(model, writer):
    # Prerequisite
    # $ sudo apt-get install libprotobuf-dev protobuf-compiler
    # $ pip3 install onnx
    print('Dump model graph...')
    dummy_input = Variable(torch.rand(config.n_batch, 4, config.width, config.width))
    if config.cuda:
        dummy_input = dummy_input.cuda()
    torch.onnx.export(model, dummy_input, "checkpoint/model.pb", verbose=False)
    writer.add_graph_onnx("checkpoint/model.pb")

def train(loader, model, cost, optimizer, epoch, writer):
    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    iou = AverageMeter()
    # Sets the module in training mode.
    model.train()
    end = time.time()
    n_step = len(loader)
    for i, data in enumerate(loader):
        # measure data loading time
        data_time.update(time.time() - end)
        # get the inputs
        inputs, labels = data['image'], data['label']
        if config.cuda:
            inputs, labels = inputs.cuda(async=True), labels.cuda(async=True)
        # wrap them in Variable
        inputs, labels = Variable(inputs), Variable(labels)
        # zero the parameter gradients
        optimizer.zero_grad()
        # forward step
        outputs = model(inputs)
        loss = cost(outputs, labels)
        # measure accuracy and record loss
        batch_iou = iou_mean(outputs, labels)
        iou.update(batch_iou, inputs.size(0))
        losses.update(loss.data[0], inputs.size(0))
        # compute gradient and do backward step
        loss.backward()
        optimizer.step()
        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()
        # log to summary
        step = i + epoch * n_step
        writer.add_scalar('training/loss', loss.data[0], step)
        writer.add_scalar('training/batch_elapse', batch_time.val, step)
        writer.add_scalar('training/batch_iou', iou.val, step)
        writer.add_scalar('training/epoch_iou', iou.avg, step)
        if i % config.print_freq == 0:
            print(
                'Epoch: [{0}][{1}/{2}]\t'
                'Time: {batch_time.avg:.3f} (io: {data_time.avg:.3f})\t\t'
                'Loss: {loss.val:.4f} ({loss.avg:.4f})\t'
                'IoU: {iou.val:.3f} ({iou.avg:.3f})\t'.format(
                    epoch, i, n_step, batch_time=batch_time,
                    data_time=data_time, loss=losses, iou=iou
                )
            )


def valid(loader, model, cost, epoch, writer, n_step):
    losses = AverageMeter()
    iou = AverageMeter()
    # Sets the model in evaluation mode.
    model.eval()
    for i, data in enumerate(loader):
        # get the inputs
        inputs, labels = data['image'], data['label']
        if config.cuda:
            inputs, labels = inputs.cuda(), labels.cuda()
        # wrap them in Variable
        inputs, labels = Variable(inputs), Variable(labels)
        # forward step
        outputs = model(inputs)
        loss = cost(outputs, labels)
        # measure accuracy and record loss
        batch_iou = iou_mean(outputs, labels)
        iou.update(batch_iou, inputs.size(0))
        losses.update(loss.data[0], inputs.size(0))
    # log to summary
    step = epoch * n_step
    writer.add_scalar('CV/loss', losses.avg, step)
    writer.add_scalar('CV/epoch_iou', iou.avg, step)
    print(
        'Epoch: [{0}]\t\tcross-validation\t\t'
        'Loss: N/A    ({loss.avg:.4f})\t'
        'IoU: N/A   ({iou.avg:.3f})\t'.format(
            epoch, loss=losses, iou=iou
        )
    )

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--resume', dest='resume', action='store_true')
    parser.add_argument('--no-resume', dest='resume', action='store_false')
    parser.add_argument('--cuda', dest='cuda', action='store_true')
    parser.add_argument('--no-cuda', dest='cuda', action='store_false')
    parser.add_argument('--epoch', type=int, help='run number of epoch')
    parser.add_argument('--lr', type=float, dest='learn_rate', help='learning rate')
    parser.set_defaults(
        resume=True, cuda=config.cuda, epoch=config.n_epoch,
        learn_rate=config.learn_rate)
    args = parser.parse_args()

    # final check whether cuda is avaiable
    config.cuda = torch.cuda.is_available() and args.cuda

    main(args)
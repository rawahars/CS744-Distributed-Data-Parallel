#!/usr/bin/env python
import os
import argparse
import time

import numpy
import torch
import torch.distributed as dist
import torch.optim as optim
from torchvision import datasets, transforms

import model as mdl

device = "cpu"
torch.set_num_threads(4)


# A function which runs the training and testing of model for given number of epochs
def run(rank, size, epochs, batch_size):
    torch.manual_seed(0)
    numpy.random.seed(0)
    batch_size = int(batch_size / float(dist.get_world_size()))

    normalize = transforms.Normalize(mean=[x / 255.0 for x in [125.3, 123.0, 113.9]],
                                     std=[x / 255.0 for x in [63.0, 62.1, 66.7]])
    transform_train = transforms.Compose([
        transforms.RandomCrop(32, padding=4),
        transforms.RandomHorizontalFlip(),
        transforms.ToTensor(),
        normalize,
    ])

    transform_test = transforms.Compose([
        transforms.ToTensor(),
        normalize])
    training_set = datasets.CIFAR10(root="./data", train=True,
                                    download=True, transform=transform_train)
    train_sampler = torch.utils.data.distributed.DistributedSampler(training_set, num_replicas=size, rank=rank)
    train_loader = torch.utils.data.DataLoader(training_set,
                                               num_workers=2,
                                               batch_size=batch_size,
                                               sampler=train_sampler,
                                               shuffle=False,
                                               pin_memory=True)

    print('Size of training set is {}'.format(len(train_loader)))

    test_set = datasets.CIFAR10(root="./data", train=False,
                                download=True, transform=transform_test)
    test_loader = torch.utils.data.DataLoader(test_set,
                                              num_workers=2,
                                              batch_size=batch_size,
                                              shuffle=False,
                                              pin_memory=True)
    print('Size of test set is {}'.format(len(test_loader)))

    training_criterion = torch.nn.CrossEntropyLoss().to(device)

    model = mdl.VGG11()
    model.to(device)
    optimizer = optim.SGD(model.parameters(), lr=0.1,
                          momentum=0.9, weight_decay=0.0001)
    # running training for one epoch
    for epoch in range(epochs):
        start_time = time.time()
        train_model(model, train_loader, optimizer, training_criterion, rank)
        print('Training time after {} epoch is {}'.format(epoch + 1, (time.time() - start_time)))
        test_model(model, test_loader, training_criterion)


def train_model(model, train_loader, optimizer, criterion, rank):
    """
    model (torch.nn.module): The model created to train
    train_loader (pytorch data loader): Training data loader
    optimizer (optimizer.*): A instance of some sort of optimizer, usually SGD
    criterion (nn.CrossEntropyLoss) : Loss function used to train the network
    epoch (int): Current epoch number
    """

    iter_number = 1
    epoch_loss = 0
    forward_time = 0
    backward_time = 0
    total_time = 0
    # remember to exit the train loop at end of the epoch
    for batch_idx, (data, target) in enumerate(train_loader):
        start_time = time.time()
        optimizer.zero_grad()
        predictions = model(data)
        forward_time += (time.time() - start_time)

        start_time_backward = time.time()
        loss = criterion(predictions, target)
        loss.backward()
        average_gradients(model, rank)
        optimizer.step()
        backward_time += (time.time() - start_time_backward)
        total_time += (time.time() - start_time)

        epoch_loss += loss

        if iter_number % 20 == 0:
            epoch_loss = epoch_loss / 20
            print('Training loss after {} epochs is {}'.format(iter_number, epoch_loss))
            epoch_loss = 0
            if iter_number != 20:
                print('Forward Pass time in iter {} is {}'.format(iter_number, forward_time / 20.0))
                print('Backward Pass time in iter {} is {}'.format(iter_number, backward_time / 20.0))
                print('Average Pass time in iter {} is {}'.format(iter_number, total_time / 20.0))
            forward_time = 0
            backward_time = 0
            total_time = 0

        iter_number += 1


def average_gradients(model, rank):
    for p in model.parameters():
        if rank == 0:
            inputs = [torch.empty(p.grad.size()) for _ in range(dist.get_world_size())]
            dist.gather(p.grad, inputs)
            avg_grad = torch.mean(torch.stack(inputs), dim=0)
            outputs = [avg_grad for _ in range(dist.get_world_size())]
            dist.scatter(p.grad, outputs)
        else:
            dist.gather(p.grad)
            dist.scatter(p.grad)


def test_model(model, test_loader, criterion):
    model.eval()
    test_loss = 0
    correct = 0
    with torch.no_grad():
        for batch_idx, (data, target) in enumerate(test_loader):
            data, target = data.to(device), target.to(device)
            output = model(data)
            test_loss += criterion(output, target)
            pred = output.max(1, keepdim=True)[1]
            correct += pred.eq(target.view_as(pred)).sum().item()

    test_loss /= len(test_loader)
    print('Test set: Average loss: {:.4f}, Accuracy: {}/{} ({:.0f}%)\n'.format(
        test_loss, correct, len(test_loader.dataset),
        100. * correct / len(test_loader.dataset)))


def init_process(master, port, rank, size, fn, epochs=1, batch_size=256, backend='gloo'):
    """ Initialize the distributed environment. """
    os.environ['MASTER_ADDR'] = master
    os.environ['MASTER_PORT'] = port
    dist.init_process_group(backend, rank=rank, world_size=size)
    fn(rank, size, epochs, batch_size)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Process arguments for training')
    parser.add_argument('--master', metavar='master-address', required=True,
                        action='store', help='The IP address of Master')
    parser.add_argument('--num-nodes', metavar='total-nodes', required=True,
                        action='store', type=int, help='Total number of nodes')
    parser.add_argument('--rank', metavar='rank', required=True,
                        action='store', type=int, help='Rank of this node')
    parser.add_argument('--epochs', metavar='epochs', required=False, default=1,
                        action='store', type=int, help='Number of epochs')

    args = parser.parse_args()
    rank = args.rank
    size = args.num_nodes
    master = args.master
    total_epochs = args.epochs
    port = '6585'
    batch_size = 256

    init_process(master, port, rank, size, run, total_epochs, batch_size)

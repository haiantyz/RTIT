#!/usr/bin/env python
# -*- coding: utf-8 -*-
import os
import sys
import copy
import math
import numpy as np
import torch
import torch.nn as nn
import torch.nn.init as init
import torch.nn.functional as F


def knn(x, k):
    inner = -2*torch.matmul(x.transpose(2, 1), x)
    xx = torch.sum(x**2, dim=1, keepdim=True)
    pairwise_distance = -xx - inner - xx.transpose(2, 1)
 
    idx = pairwise_distance.topk(k=k, dim=-1)[1]   # (batch_size, num_points, k)
    return idx


def get_graph_feature(x, k=20, idx=None, dim9=False):
    batch_size = x.size(0)
    num_points = x.size(2)
    x = x.view(batch_size, -1, num_points)
    if idx is None:
        if dim9 == False:
            idx = knn(x, k=k).cuda(0)   # (batch_size, num_points, k) # rqh, original: cuda(2)
        else:
            idx = knn(x[:, 6:], k=k)
    device = torch.device('cuda:0') # rqh, original: cuda:0

    idx_base = torch.arange(0, batch_size, device=device).view(-1, 1, 1)*num_points

    idx = idx + idx_base

    idx = idx.view(-1)
 
    _, num_dims, _ = x.size()

    x = x.transpose(2, 1).contiguous()   # (batch_size, num_points, num_dims)  -> (batch_size*num_points, num_dims) #   batch_size * num_points * k + range(0, batch_size*num_points)
    feature = x.view(batch_size*num_points, -1)[idx, :]
    feature = feature.view(batch_size, num_points, k, num_dims) 
    x = x.view(batch_size, num_points, 1, num_dims).repeat(1, 1, k, 1)
    
    feature = torch.cat((feature-x, x), dim=3).permute(0, 3, 1, 2)
  
    return feature      # (batch_size, 2*num_dims, num_points, k)


class attention_point(nn.Module):
    def __init__(self,input_channel):
        super(attention_point, self).__init__()

        self.conv1 = nn.Sequential(nn.Conv1d(input_channel, 1, kernel_size=1, bias=False),nn.LeakyReLU(negative_slope=0.2))
        
        self.softmax=nn.Softmax(dim=2)


    def forward(self, x):
        batchsize = x.size()[0]   #bxcxn
        channel_num=x.size()[1]
        point_initial=x.size()[2]
        
        net=x
        net=self.conv1(net)   #bx1xn

        net=self.softmax(net)   #bx1xn

        x = x+net
                
        return x

def batch_quat2mat(batch_quat):
    '''

    :param batch_quat: shape=(B, 4)
    :return: Rotation matrix
    '''
    w, x, y, z = batch_quat[:, 0], batch_quat[:, 1], batch_quat[:, 2], \
                 batch_quat[:, 3]
    device = batch_quat.device
    B = batch_quat.size()[0]
    R = torch.zeros(dtype=torch.float, size=(B, 3, 3)).to(device)
    R[:, 0, 0] = 1 - 2 * y * y - 2 * z * z
    R[:, 0, 1] = 2 * x * y - 2 * z * w
    R[:, 0, 2] = 2 * x * z + 2 * y * w
    R[:, 1, 0] = 2 * x * y + 2 * z * w
    R[:, 1, 1] = 1 - 2 * x * x - 2 * z * z
    R[:, 1, 2] = 2 * y * z - 2 * x * w
    R[:, 2, 0] = 2 * x * z - 2 * y * w
    R[:, 2, 1] = 2 * y * z + 2 * x * w
    R[:, 2, 2] = 1 - 2 * x * x - 2 * y * y
    return R

class STN3d(nn.Module):
    def __init__(self, args):
        super(STN3d, self).__init__()
        self.args = args
        self.conv1 = torch.nn.Conv1d(3, 64, 1)
        self.conv2 = torch.nn.Conv1d(64, 128, 1)
        self.conv3 = torch.nn.Conv1d(128, 1024, 1)
        self.fc1 = nn.Linear(1024, 512)
        self.fc2 = nn.Linear(512, 256)
        self.fc3 = nn.Linear(256, 4)

        self.bn1 = nn.BatchNorm1d(64)
        self.bn2 = nn.BatchNorm1d(128)
        self.bn3 = nn.BatchNorm1d(1024)
        self.bn4 = nn.BatchNorm1d(512)
        self.bn5 = nn.BatchNorm1d(256)

        self.attention_point=attention_point(128)

    def forward(self, x):
        batchsize = x.size()[0]   #bx3xn

        x = F.leaky_relu(self.bn1(self.conv1(x)), negative_slope=0.2)
        x = F.leaky_relu(self.bn2(self.conv2(x)), negative_slope=0.2)
        x = self.attention_point(x)
        x = F.leaky_relu(self.bn3(self.conv3(x)), negative_slope=0.2)
        x = torch.max(x, 2, keepdim=True)[0]
        x = x.view(-1, 1024)

        x = F.leaky_relu(self.bn4(self.fc1(x)), negative_slope=0.2)
        x = F.leaky_relu(self.bn5(self.fc2(x)), negative_slope=0.2)
        x = self.fc3(x)

        x = F.normalize(x, dim=1)

        rotation = batch_quat2mat(x)

        return rotation


class DGCNN_cls(nn.Module):
    def __init__(self, args):
        super(DGCNN_cls, self).__init__()
        self.args = args
        self.k = self.args.k
        if self.args.dataset == 'scanobject' :
            self.output_channels = 15
        elif self.args.dataset == 'modelnet40' : 
            self.output_channels = 40
        else : self.output_channels = 10

        if self.args.fa:
            self.stn = STN3d(args)
        
        self.bn1 = nn.BatchNorm2d(64)
        self.bn2 = nn.BatchNorm2d(64)
        self.bn3 = nn.BatchNorm2d(128)
        self.bn4 = nn.BatchNorm2d(256)
        self.bn5 = nn.BatchNorm1d(args.emb_dims)

        self.conv1 = nn.Sequential(nn.Conv2d(6, 64, kernel_size=1, bias=False),
                                   self.bn1,
                                   nn.LeakyReLU(negative_slope=0.2))

        self.conv2 = nn.Sequential(nn.Conv2d(64*2, 64, kernel_size=1, bias=False),
                                   self.bn2,
                                   nn.LeakyReLU(negative_slope=0.2))
        self.conv3 = nn.Sequential(nn.Conv2d(64*2, 128, kernel_size=1, bias=False),
                                   self.bn3,
                                   nn.LeakyReLU(negative_slope=0.2))
        self.conv4 = nn.Sequential(nn.Conv2d(128*2, 256, kernel_size=1, bias=False),
                                   self.bn4,
                                   nn.LeakyReLU(negative_slope=0.2))
        self.conv5 = nn.Sequential(nn.Conv1d(512, args.emb_dims, kernel_size=1, bias=False),
                                   self.bn5,
                                   nn.LeakyReLU(negative_slope=0.2))
        self.linear1 = nn.Linear(args.emb_dims*2, 512, bias=False)
        self.bn6 = nn.BatchNorm1d(512)
        self.dp1 = nn.Dropout(p=args.dropout)
        self.linear2 = nn.Linear(512, 256)
        self.bn7 = nn.BatchNorm1d(256)
        self.dp2 = nn.Dropout(p=args.dropout)
        self.linear3 = nn.Linear(256, self.output_channels)

    def forward(self, x):
        batch_size = x.size(0)

        if self.args.fa:
            rotation = self.stn(x)
            x = torch.matmul(rotation, x)

        x = get_graph_feature(x, k=self.k)      # (batch_size, 3, num_points) -> (batch_size, 3*2, num_points, k)
        x = self.conv1(x)                       # (batch_size, 3*2, num_points, k) -> (batch_size, 64, num_points, k)
        x1 = x.max(dim=-1, keepdim=False)[0]    # (batch_size, 64, num_points, k) -> (batch_size, 64, num_points)

        x = get_graph_feature(x1, k=self.k)     # (batch_size, 64, num_points) -> (batch_size, 64*2, num_points, k)
        x = self.conv2(x)                       # (batch_size, 64*2, num_points, k) -> (batch_size, 64, num_points, k)
        x2 = x.max(dim=-1, keepdim=False)[0]    # (batch_size, 64, num_points, k) -> (batch_size, 64, num_points)

        x = get_graph_feature(x2, k=self.k)     # (batch_size, 64, num_points) -> (batch_size, 64*2, num_points, k)
        x = self.conv3(x)                       # (batch_size, 64*2, num_points, k) -> (batch_size, 128, num_points, k)
        x3 = x.max(dim=-1, keepdim=False)[0]    # (batch_size, 128, num_points, k) -> (batch_size, 128, num_points)

        x = get_graph_feature(x3, k=self.k)     # (batch_size, 128, num_points) -> (batch_size, 128*2, num_points, k)
        x = self.conv4(x)                       # (batch_size, 128*2, num_points, k) -> (batch_size, 256, num_points, k)
        x4 = x.max(dim=-1, keepdim=False)[0]    # (batch_size, 256, num_points, k) -> (batch_size, 256, num_points)

        x = torch.cat((x1, x2, x3, x4), dim=1)  # (batch_size, 64+64+128+256, num_points)

        x = self.conv5(x)                       # (batch_size, 64+64+128+256, num_points) -> (batch_size, emb_dims, num_points)
        x1 = F.adaptive_max_pool1d(x, 1).view(batch_size, -1)           # (batch_size, emb_dims, num_points) -> (batch_size, emb_dims)
        x2 = F.adaptive_avg_pool1d(x, 1).view(batch_size, -1)           # (batch_size, emb_dims, num_points) -> (batch_size, emb_dims)
        x = torch.cat((x1, x2), 1)              # (batch_size, emb_dims*2)

        x = F.leaky_relu(self.bn6(self.linear1(x)), negative_slope=0.2) # (batch_size, emb_dims*2) -> (batch_size, 512)
        x = self.dp1(x)
        x = F.leaky_relu(self.bn7(self.linear2(x)), negative_slope=0.2) # (batch_size, 512) -> (batch_size, 256)
        x = self.dp2(x)
        x = self.linear3(x)                                             # (batch_size, 256) -> (batch_size, output_channels)
        
        return x

 
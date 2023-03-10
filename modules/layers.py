import numpy as np
import torch
import torch.nn as nn
from scipy.sparse import csr_matrix
from torch_geometric.nn import MessagePassing
from torch_geometric.utils import add_self_loops, degree


class REGCNLayer(nn.Module):
    def __init__(self, input_dim, output_dim, bias=False, active='rrelu', dtype=torch.float64):
        """
        :param input_dim:
        :param output_dim:
        :param bias:
        :param active:
        """
        super(REGCNLayer, self).__init__()
        self.dtype = dtype
        self.input_dim = input_dim
        self.output_dim = output_dim
        self.fc_self = nn.Linear(input_dim, output_dim, bias=bias, dtype=dtype)
        self.fc_aggregate = nn.Linear(input_dim, output_dim, bias=bias, dtype=dtype)
        if active == 'rrelu':
            self.active = nn.RReLU()
        elif active == 'sigmoid':
            self.active = nn.Sigmoid()
        elif active == 'tanh':
            self.active = nn.Tanh()

    def calculate_message(self, src, rela):
        return self.fc_aggregate(src + rela)

    def aggregate(self, message, num_node, des):
        des_unique, count = torch.unique(des, return_counts=True)
        index_matrix = csr_matrix((np.array(range(des_unique.shape[0]), dtype='int64'),
                                   (des_unique, np.zeros(des_unique.shape[0], dtype='int64'))),
                                  shape=(num_node, 1))
        index = torch.zeros(message.shape[0], self.output_dim, dtype=torch.int64) + index_matrix[des].todense()
        message = torch.zeros(des_unique.shape[0], self.output_dim, dtype=self.dtype).scatter_(0, index, message,
                                                                                               reduce='add')
        return des_unique, message / (count.reshape(des_unique.shape[0], 1))

    def forward(self, nodes_embed, edges_embed, edges):
        """
        :param nodes_embed:Tensor, size=(num_node,input_dim)
        :param edges_embed: Tensor,size=(num_edge,input_dim)
        :param edge: Tensor, size=(num_edge, 3), with the format of (source node, edge, destination node)
        :return: the representation of node after aggregation
        """
        # self loop
        h = self.fc_self(nodes_embed)
        # calculate message
        message = self.calculate_message(nodes_embed[edges[:, 0]], edges_embed[edges[:, 1]])
        # aggregate
        des_index, message = self.aggregate(message, nodes_embed.shape[0], edges[:, 2])
        # send message
        h[des_index] = h[des_index] + message
        return self.active(h)


class WGCNLayer(nn.Module):
    def __init__(self, num_relation, input_dim, output_dim, active='relu', bias=False, dtype=torch.float):
        super(WGCNLayer, self).__init__()
        self.num_relation = num_relation
        self.input_dim = input_dim
        self.dtype = dtype
        self.output_dim = output_dim
        self.relation_weight = nn.Parameter(torch.rand((num_relation, 1), dtype=dtype))
        self.fc = nn.Linear(input_dim, output_dim, bias=bias, dtype=dtype)
        if active == 'sigmoid':
            self.active = nn.Sigmoid()
        elif active == 'tanh':
            self.active = nn.Tanh()
        else:
            self.active = nn.ReLU()

    def calculate_message(self, src, relation_weight):
        return self.fc(src * relation_weight)

    def aggregate(self, message, num_node, des):
        des_unique, count = torch.unique(des, return_counts=True)
        index_matrix = csr_matrix((np.array(range(des_unique.shape[0]), dtype='int64'),
                                   (des_unique, np.zeros(des_unique.shape[0], dtype='int64'))),
                                  shape=(num_node, 1))
        index = torch.zeros(message.shape[0], message.shape[1], dtype=torch.int64) + index_matrix[des].todense()
        message = torch.zeros(des_unique.shape[0], self.output_dim, dtype=self.dtype).scatter_(0, index, message,
                                                                                               reduce='add')
        return des_unique, message

    def forward(self, nodes_embed, edges):
        """
        :param nodes_embed: Tensor, the embedding of nodes, size=(num_node,input_dim)
        :param edges: Tensor, size=(num_edge, 3), with the format of (source node, edge, destination node)
        :return: new representation of nodes
        """
        h = self.fc(nodes_embed)
        message = self.calculate_message(nodes_embed[edges[:, 0]], self.relation_weight[edges[:, 1]])
        des_index, message = self.aggregate(message, nodes_embed.shape[0], edges.shape[0], edges[:, 2])
        h[des_index] = h[des_index] + message
        return self.active(h)


class GCNLayer(MessagePassing):
    def __init__(self, in_channels, out_channels):
        super(GCNLayer, self).__init__(aggr='add')
        self.lin = torch.nn.Linear(in_channels, out_channels)

    def forward(self, node_embed, edges):
        """
        :param node_embed: Tensor, size=(num_node, input_dim), Embeddings of nodes
        :param edge_index: LongTensor ,size=(num_edge, 2), source nodes and destination nodes
        :return:
        """
        # self loop
        edges, _ = add_self_loops(edges, num_nodes=node_embed.size(0))
        deg = degree(edges[0], node_embed.size(0), dtype=node_embed.dtype)
        deg_inv_sqrt = deg.pow(-0.5)
        deg_inv_sqrt[torch.isinf(deg_inv_sqrt)] = 0
        # normalization coefficient
        norm = deg_inv_sqrt[edges[0]] * deg_inv_sqrt[edges[1]]
        # propagate message
        return self.propagate(edges, x=node_embed, norm=norm)

    def message(self, x_j, norm):
        # message passing
        return norm.view(-1, 1) * x_j

    def update(self, aggr_out):
        # update presentation of nodes
        return self.lin(aggr_out)


class CompGCNLayer(nn.Module):
    def __init__(self, input_dim, output_dim, num_rela, dtype=torch.float):
        super(CompGCNLayer, self).__init__()
        self.output_dim = output_dim
        self.num_rela = num_rela
        self.dtype = dtype
        self.W_o = nn.Linear(input_dim, output_dim, bias=False)
        self.W_i = nn.Linear(input_dim, output_dim, bias=False)
        self.W_s = nn.Linear(input_dim, output_dim, bias=False)
        self.W_r = nn.Linear(input_dim, output_dim, bias=False)

    def composition(self, node_embed, rela_embed, mode='add'):
        if mode == 'add':
            res = node_embed + rela_embed
        elif mode == 'sub':
            res = node_embed - rela_embed
        elif mode == 'mult':
            res = node_embed * rela_embed
        else:
            res = None
        return res

    def aggregate(self, message, des):
        des_unique, des_index = torch.unique(des, return_inverse=True)
        message = torch.zeros(des_unique.shape[0], message.shape[1], dtype=self.dtype).scatter_add_(
            0, des_index.unsqueeze(1).expand_as(message), message)
        return des_unique, message

    def forward(self, node_embed, rela_embed, edges, mode='add'):
        """
        :param node_embed:
        :param rela_embed:
        :param edges: LongTensor, including the original edge and reversed edge
        :param mode: Method to composite representations of relations and nodes
        :return:
        """
        # self loop
        h_v = self.W_i(self.composition(node_embed, rela_embed[self.num_rela * 2], mode))

        # original edges
        index = edges[:, 1] < self.num_rela
        src = edges[index][:, 0]
        rela = edges[index][:, 1]
        des = edges[index][:, 2]
        index_matrix = torch.zeros(node_embed.shape[0], dtype=torch.long)
        index_matrix[des] = torch.arange(des.shape[0], dtype=torch.long)
        message = self.W_o(self.composition(node_embed[src], rela_embed[rela]))
        message = message[index_matrix[des]]
        des_index, message = self.aggregate(message, des)
        h_v[des_index] = h_v[des_index] + message

        # reversed edges
        index = edges[:, 1] >= self.num_rela
        src = edges[index][:, 0]
        rela = edges[index][:, 1]
        des = edges[index][:, 2]
        index_matrix[des] = torch.arange(des.shape[0], dtype=torch.long)
        message = self.W_s(self.composition(node_embed[src], rela_embed[rela]))
        message = message[index_matrix[des]]
        des_index, message = self.aggregate(message, des)
        h_v[des_index] = h_v[des_index] + message

        # update relation representation
        h_r = self.W_r(rela_embed)
        return h_v, h_r


class RGCNLayer(nn.Module):
    def __init__(self, input_dim, output_dim, num_rels):
        super(RGCNLayer, self).__init__()
        self.input_dim = input_dim
        self.output_dim = output_dim
        self.num_rels = num_rels
        self.weight = nn.Parameter(torch.Tensor(num_rels, input_dim, output_dim))
        self.self_loop_weigt = nn.Parameter(torch.Tensor(input_dim, output_dim))
        nn.init.xavier_uniform_(self.weight, gain=nn.init.calculate_gain('relu'))

    def aggregate(self, message, des):
        des_unique, des_index, count = torch.unique(des, return_inverse=True, return_counts=True)
        message = torch.zeros(des_unique.shape[0], message.shape[1], dtype=message.dtype).scatter_add_(
            0, des_index.unsqueeze(1).expand_as(message), message)
        return des_unique, message / count.reshape(-1, 1)

    def forward(self, h, edges):
        """
        :param h: node embeddings, shape (num_nodes, input_dim)
        :param edges: list of triplets (src, rel, dst)
        :return: new node embeddings, shape (num_nodes, output_dim)
        """
        # separate triplets into src, rel, dst
        src, rel, dst = edges.transpose(0, 1)
        # gather node embeddings by indices
        src_h = h[src]
        # gather relation weights by indices
        weight = self.weight[rel]
        index_matrix = torch.zeros(h.shape[0], dtype=torch.long)
        index_matrix[dst] = torch.arange(dst.shape[0], dtype=torch.long)
        msg = torch.bmm(src_h.unsqueeze(1), weight).squeeze(1)
        # sort message corresponding to destination node
        msg = msg[index_matrix[dst]]
        # aggregate message
        dst_index, msg = self.aggregate(msg, dst)
        # self loop message passing
        out = torch.mm(h, self.self_loop_weigt)
        # compose message
        out[dst_index] = out[dst_index] + msg
        return out




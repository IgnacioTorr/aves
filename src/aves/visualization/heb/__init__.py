import graph_tool
import graph_tool.topology
import graph_tool.draw
import graph_tool.inference
import graph_tool.search

import numpy as np
import seaborn as sns
import pandas as pd

import typing

from aves.features.geometry import bspline
from aves.visualization.networks import NodeLink
from cytoolz import keyfilter, valfilter, unique, valmap, sliding_window, groupby, pluck
from collections import defaultdict

from matplotlib.collections import LineCollection
import matplotlib.colors as colors
from matplotlib.patches import FancyArrowPatch, Wedge
from matplotlib.collections import PatchCollection, LineCollection
from sklearn.preprocessing import MinMaxScaler


class HierarchicalEdgeBundling(object):
    def __init__(self, nodelink: NodeLink, state=None, covariate_type=None):
        self.nodelink = nodelink
        self.network = nodelink.network
        self.state = state
        if state is not None:
            self.block_levels = self.state.get_bs()
        else:
            self.estimate_blockmodel(covariate_type=covariate_type)
            
        self.build_community_graph()
        self.build_node_memberships()
        self.build_edges()
        self.set_community_level(0)

        
    def estimate_blockmodel(self, covariate_type='real-exponential'):
        if self.nodelink.edge_weight is not None and covariate_type is not None:
            state_args = dict(recs=[self.nodelink.edge_weight], rec_types=[covariate_type])
            self.state = graph_tool.inference.minimize_nested_blockmodel_dl(self.network, state_args=state_args)
        else:
            self.state = graph_tool.inference.minimize_nested_blockmodel_dl(self.network)
            
        self.block_levels = self.state.get_bs()

    def get_node_memberships(self, level):
        return [self.membership_per_level[level][int(node_id)] for node_id in self.network.vertices()]
      
    def build_community_graph(self):
        tree, membership, order = graph_tool.inference.nested_blockmodel.get_hierarchy_tree(self.state, empty_branches=False)
        self.nested_graph = tree
        self.nested_graph.set_directed(False)
        
        self.radial_positions = np.array(list(graph_tool.draw.radial_tree_layout(self.nested_graph, self.nested_graph.num_vertices() - 1)))
        self.nodelink.set_node_positions(self.radial_positions[:self.network.num_vertices()])
        
        self.node_angles = np.degrees(np.arctan2(self.nodelink.node_positions_vector[:,1], self.nodelink.node_positions_vector[:,0]))
        self.node_angles_dict = dict(zip(map(int, self.network.vertices()), self.node_angles))
        self.node_ratio = np.sqrt(np.dot(self.radial_positions[0], self.radial_positions[0]))
        
        self.community_graph = graph_tool.GraphView(self.nested_graph, directed=True, vfilt=lambda x: x >= self.network.num_vertices())
        self.community_nodelink = NodeLink(self.community_graph)
        self.community_nodelink.set_node_positions(self.radial_positions[self.network.num_vertices():])

    def build_node_memberships(self):
        self.nested_graph.set_directed(True)

        depth_edges =  graph_tool.search.dfs_iterator(self.nested_graph, source=self.nested_graph.num_vertices() - 1, array=True)

        self.membership_per_level = defaultdict(lambda: defaultdict(int))

        stack = []
        for src_idx, dst_idx in depth_edges:
            if not stack:       
                stack.append(src_idx)

            if dst_idx < self.network.num_vertices():
                # leaf node
                path = [dst_idx]
                path.extend(reversed(stack))
                
                for level, community_id in enumerate(path):
                    self.membership_per_level[level][dst_idx] = community_id
            else:
                while src_idx != stack[-1]:
                    # a new community, remove visited branches
                    stack.pop()

                stack.append(dst_idx)

        self.nested_graph.set_directed(False)
        
    def set_community_level(self, level=1):        
        self.community_ids = list(unique(map(int, self.membership_per_level[level].values())))
        self.community_level = level
        
        self.prepare_segments()

    def check_status(self):
        if getattr(self, 'community_level', None) is None:
            self.set_community_level(0)

    def edge_to_spline(self, src, dst, n_points=100, smoothing_factor=0.8):
        if src == dst:
            raise Exception('Self-pointing edges are not supported')

        vertex_path, edge_path = graph_tool.topology.shortest_path(self.nested_graph, src, dst)
        edge_cp = [self.radial_positions[int(node_id)] for node_id in vertex_path]

        try:    
            smooth_edge = bspline(edge_cp, degree=min(len(edge_cp) - 1, 3), n=n_points)
            source_edge = np.vstack((np.linspace(edge_cp[0][0], edge_cp[-1][0], num=n_points, endpoint=True),
                                     np.linspace(edge_cp[0][1], edge_cp[-1][1], num=n_points, endpoint=True))).T

            if smoothing_factor < 1.0:
                smooth_edge = smooth_edge * smoothing_factor + source_edge * (1.0 - smoothing_factor)

            return smooth_edge
        except ValueError:
            print(src, dst, 'error')
            return None
        
        
    def build_edges(self, n_points=50):
        self.edges = []
        
        self.n_points = n_points

        for e in self.nodelink.edge_data:                
            src = e.index_pair[0]
            dst = e.index_pair[1]

            edge = self.edge_to_spline(src, dst, n_points=n_points)
                        
            if edge is not None:
                if self.nodelink.edge_weight is not None:
                    weight = self.nodelink.edge_weight[e.handle]
                else:
                    weight = 1.0
                    
                e.polyline = edge

                self.edges.append({
                    'spline': edge,
                    'source': e.index_pair[0],
                    'target': e.index_pair[1],
                    'weight': weight
                })
        
    def prepare_segments(self, level=None):
        self.segments_per_pair = defaultdict(list)
        
        if level is None:
            level = self.community_level
        
        for edge_data in self.edges:
            segments = list(sliding_window(2, edge_data['spline']))
            values = np.linspace(0, 1, num=self.n_points - 1)
            pair = (self.membership_per_level[level][edge_data['source']],
                    self.membership_per_level[level][edge_data['target']])
            #print(pair)
            #break
            
            self.segments_per_pair[pair].append((segments, values, edge_data['weight']))
            
            
    def plot_edges(self, ax, linewidth=1, alpha=0.25, min_linewidth=None, linestyle='solid', level=None, palette='plasma'):
        self.check_status()
        
        if level is None:
            level = self.community_level if self.community_level else 0
            
        if level != self.community_level:
            self.prepare_segments(level=level)
            
        community_ids = sorted(set(self.membership_per_level[level].values()))
        community_colors = dict(zip(community_ids, sns.color_palette(palette, n_colors=len(community_ids))))
        
        if self.nodelink.edge_weight is not None and min_linewidth is not None:
            width_scaler = MinMaxScaler(feature_range=(min_linewidth, linewidth))
            width_scaler.fit(np.sqrt(self.nodelink.edge_weight.a).reshape(-1, 1))
        else:
            width_scaler = None
            
        for pair, seg_and_val in self.segments_per_pair.items():
            #print(self.community_colors)
            #print(pair)
            cmap = colors.LinearSegmentedColormap.from_list("", [community_colors[pair[0]], community_colors[pair[1]]])

            all_segments = np.concatenate([s[0] for s in seg_and_val])
            all_values = np.concatenate([s[1] for s in seg_and_val])
            all_weights = np.concatenate([np.repeat(s[2], self.n_points) for s in seg_and_val])
            
            if width_scaler is not None:
                linewidths = np.squeeze(width_scaler.transform(np.sqrt(np.array(all_weights)).reshape(-1, 1)))
            else:
                linewidths = linewidth

            edge_collection = LineCollection(all_segments, cmap=cmap, linewidths=linewidths, linestyle=linestyle, alpha=alpha)
            edge_collection.set_array(all_values)
            ax.add_collection(edge_collection)

    def plot_nodes(self, *args, **kwargs):
        self.check_status()
        self.nodelink.plot_nodes(*args, **kwargs)
        
    def plot_community_wedges(self, ax, level=None, wedge_width=0.5, wedge_ratio=None, wedge_offset=0.05, alpha=1.0, fill_gaps=False, palette='plasma'):
        self.check_status()
        
        if wedge_ratio is None:
            wedge_ratio = self.node_ratio + wedge_offset
            
        if level is None:
            level = self.community_level
            
        community_ids = sorted(set(self.membership_per_level[level].values()))
        community_colors = dict(zip(community_ids, sns.color_palette(palette, n_colors=len(community_ids))))

        wedge_meta = []
        wedge_gap = 180 / self.network.num_vertices() if fill_gaps else 0

        for c_id in community_ids:
            
            nodes_in_community = list(valfilter(lambda x: x == c_id, self.membership_per_level[level]).keys())

            community_angles = [self.node_angles_dict[n_id] for n_id in nodes_in_community]
            community_angles = [a if a >= 0 else a + 360 for a in community_angles]
            community_angle = self.node_angles[int(c_id)]
            
            if community_angle < 0:
                community_angle += 360
                
            min_angle = min(community_angles)
            max_angle = max(community_angles)
                                    
            extent_angle = max_angle - min_angle
            
            if extent_angle < 0:
                min_angle, max_angle = max_angle, min_angle
            
            if fill_gaps:
                min_angle -= wedge_gap
                max_angle += wedge_gap
            
            wedge_meta.append({'community_id': c_id,
                               'n_nodes': len(nodes_in_community),
                               'center_angle': community_angle,
                               'extent_angle': extent_angle,
                               'min_angle': min_angle,
                               'max_angle': max_angle,
                               'color': community_colors[c_id]})
        
                    
        collection = [Wedge(0.0, wedge_ratio + wedge_width, w['min_angle'], w['max_angle'], width=wedge_width) for w in wedge_meta]
        ax.add_collection(PatchCollection(collection, edgecolor='none', 
                                          color=[w['color'] for w in wedge_meta],
                                          alpha=alpha))
        
        return wedge_meta, collection
        
        
    def n_communities(self):
        return len(self.community_ids)
    
    
    def plot_node_labels(self, ax, label_func=None, ratio=None, offset=0.1):
        self.check_status()
        if ratio is None:
            ratio = self.node_ratio
            
        ratio += offset
            
        for node_id, angle_degrees, pos in zip(self.network.vertices(), self.node_angles, self.radial_positions):
            node_id = int(node_id)
            label = label_func(node_id) if label_func else str(node_id)
            angle = np.radians(angle_degrees)

            if pos[0] >= 0:
                ha = 'left'
            else:
                ha = 'right'
            
            text_rotation = angle_degrees

            if text_rotation > 90:
                text_rotation = text_rotation - 180
            elif text_rotation < -90:
                text_rotation = text_rotation + 180

            ax.annotate(label, 
                         (ratio * np.cos(angle), ratio * np.sin(angle)), 
                         rotation=text_rotation, ha=ha, va='center', rotation_mode='anchor',
                         fontsize='small')    
            
    def plot_community_labels(self, ax, level=None, ratio=None, offset=0.05):
        self.check_status()
        
        if ratio is None:
            ratio = self.node_ratio + offset
            
        if level is None:
            level = self.community_level if self.community_level else 0
            
        community_ids = set(self.membership_per_level[level].values())

        for c_id in community_ids:
            
            nodes_in_community = list(valfilter(lambda x: x == c_id, self.membership_per_level[level]).keys())

            community_angles = [self.node_angles_dict[n_id] for n_id in nodes_in_community]
            community_angles = [a if a >= 0 else a + 360 for a in community_angles]
            community_angle = self.node_angles[int(c_id)]
            
            if community_angle < 0:
                community_angle += 360
                
            min_angle = min(community_angles)
            max_angle = max(community_angles)
                                    
            mid_angle = 0.5 * (max_angle + min_angle)
            mid_angle_radians = np.radians(mid_angle)
            
            pos_x, pos_y = ratio * np.cos(mid_angle_radians), ratio * np.sin(mid_angle_radians)
            
            ha = 'left' if pos_x >= 0 else 'right'
            
            if mid_angle > 90:
                mid_angle = mid_angle - 180
            elif mid_angle < -90:
                mid_angle = mid_angle + 180

            ax.annotate(f'{c_id}', 
                         (pos_x, pos_y), 
                         rotation=mid_angle, ha=ha, va='center', rotation_mode='anchor',
                         fontsize='small')   

    def plot_community_network(self, ax):
        self.community_nodelink.plot_nodes(ax, marker='s')
        self.community_nodelink.plot_edges(ax, with_arrows=True)
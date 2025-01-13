# coding=utf-8
# Copyright 2024 The Google Research Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""JODIE model with Structural Mapping Capability."""

import torch
from torch_geometric import data as torch_geo_data

from fm4tlp.models import model_config_pb2
from fm4tlp.models import model_template
from fm4tlp.modules import decoder
from fm4tlp.modules import emb_module
from fm4tlp.modules import memory_module
from fm4tlp.modules import message_agg
from fm4tlp.modules import message_func
from fm4tlp.modules import neighbor_loader
from fm4tlp.modules import structural_mapper
from fm4tlp.utils import utils


class JODIE(model_template.TlpModel):
  """JODIE model with structural mapping capability."""

  def __init__(
      self,
      model_config,
      total_num_nodes,
      raw_message_size,
      device,
      structural_feature_dim = 0,
      structural_feature_mean = list(),
      structural_feature_std = list(),
  ):  # pylint: disable=useless-super-delegation
    """Initializes the model."""
    super().__init__(
        model_config,
        total_num_nodes,
        raw_message_size,
        device,
        structural_feature_dim,
        structural_feature_mean,
        structural_feature_std
    )

  def save_model(self, model_path):
    torch.save(
        {
            model_name: model_components.state_dict()
            for model_name, model_components in self._model_components.items()
        },
        model_path,
    )
    utils.save_structural_feature_measurement(
        model_path,
        "structural_feature_mean",
        self._structural_feature_mean,
    )
    utils.save_structural_feature_measurement(
        model_path,
        "structural_feature_std",
        self._structural_feature_std,
    )

  def load_model(self, model_path):
    checkpoint = torch.load(model_path)
    for model_name, model_component in self._model_components.items():
      model_component.load_state_dict(checkpoint[model_name])

    self._structural_feature_mean = utils.load_structural_feature_measurement(
        model_path,
        "structural_feature_mean",
    )
    self._structural_feature_std = utils.load_structural_feature_measurement(
        model_path,
        "structural_feature_std",
    )

  def _initialize_model(self):
    # define the model end-to-end
    self._memory = memory_module.TGNMemory(
        self._total_num_nodes,
        self._raw_message_size,
        self._config.memory_dimension,
        self._config.time_dimension,
        message_module=message_func.IdentityMessage(
            self._raw_message_size,
            self._config.memory_dimension,
            self._config.time_dimension,
        ),
        aggregator_module=message_agg.LastAggregator(),
        memory_updater_cell="rnn",
    ).to(self._device)

    self._gnn = emb_module.TimeEmbedding(
        in_channels=self._config.memory_dimension,
        out_channels=self._config.embedding_dimension,
    ).to(self._device)

    self._link_pred = decoder.LinkPredictor(
        in_channels=self._config.embedding_dimension
    ).to(self._device)

    self._model_components = {
        "memory": self._memory,
        "gnn": self._gnn,
        "link_pred": self._link_pred,
    }

    self._optimizer = torch.optim.Adam(
        set(self._memory.parameters())
        | set(self._gnn.parameters())
        | set(self._link_pred.parameters()),
        lr=self._config.learning_rate,
    )

    self._criterion = torch.nn.BCEWithLogitsLoss()

    if self._config.structural_mapping_hidden_dim > 0:
      self._struct_mapper = structural_mapper.StructMapper(
          structural_feature_dim=self._structural_feature_dim,
          hidden_dim=self._config.structural_mapping_hidden_dim,
          memory_emb_dim=self._config.memory_dimension,
      ).to(self._device)

      self._model_components.update({"struct_mapper": self._struct_mapper})

      self._optimizer.add_param_group(
          {"params": self._struct_mapper.parameters()}
      )

      self._criterion_struct = torch.nn.MSELoss()

    # Helper vector to map global node indices to local ones.
    self._assoc = torch.empty(
        self._total_num_nodes, dtype=torch.long, device=self._device
    )

  def optimize(self, loss):
    loss.backward()
    self._optimizer.step()
    self._memory.detach()

  def update_memory(
      self,
      *,
      source_nodes,
      target_nodes_pos,
      target_nodes_neg,
      timestamps,
      messages,
      last_neighbor_loader,
      data,
  ):
    del target_nodes_neg, last_neighbor_loader, data  # Unused.
    self._memory.update_state(
        src=source_nodes,
        dst=target_nodes_pos,
        t=timestamps,
        raw_msg=messages,
    )

  def reset_memory(self):
    self._memory.reset_state()

  @property
  def has_memory(self):
    return True

  @property
  def has_struct_mapper(self):
    return self._config.structural_mapping_hidden_dim > 0

  def initialize_train(self):
    self._memory.train()
    self._gnn.train()
    self._link_pred.train()
    if self.has_struct_mapper:
      self._struct_mapper.train()

  def initialize_test(self):
    """Initializes test evaluation."""
    self._memory.eval()
    self._gnn.eval()
    self._link_pred.eval()
    if self.has_struct_mapper:
      self._struct_mapper.eval()

  def initialize_batch(self, batch):
    """Initializes batch processing."""
    batch.to(self._device)
    self._optimizer.zero_grad()

  def compute_loss(
      self,
      model_prediction,
      predicted_memory_emb,
      memory_emb,
  ):
    """Computes the loss from a model prediction."""
    model_loss = self._criterion(
        model_prediction.y_pred_pos,
        torch.ones_like(model_prediction.y_pred_pos),
    )
    if model_prediction.y_pred_neg is not None:
      model_loss += self._criterion(
          model_prediction.y_pred_neg,
          torch.zeros_like(model_prediction.y_pred_neg),
      )
    structmap_loss = 0.0
    if self.has_struct_mapper:
      structmap_loss += (
          self._criterion_struct(predicted_memory_emb, memory_emb)
          * self._config.alpha
      )
    return model_loss, structmap_loss

  def predict_on_edges(
      self,
      *,
      source_nodes,
      target_nodes_pos,
      target_nodes_neg = None,
      last_neighbor_loader,
      data,
  ):
    """Generates predictions from input edges.

    Args:
      source_nodes: Source nodes.
      target_nodes_pos: Target nodes for positive edges.
      target_nodes_neg: Target nodes for negative edges.
      last_neighbor_loader: Object to load recent node neighbors.
      data: The torch geo temporal dataset object.

    Returns:
      The model prediction. y_pred_neg is None if target_nodes_neg is None.
    """
    del last_neighbor_loader  # Unused.

    all_nodes = torch.cat([source_nodes, target_nodes_pos])
    if target_nodes_neg is not None:
      all_nodes = torch.cat([all_nodes, target_nodes_neg])
    # Get a list of unique node IDs sorted in ascending order, a list of indices
    # of the input node IDs in the unique node ID list, and a list of counts of
    # the number of times each unique node ID appears in the input node ID list.
    n_id, n_idx, n_counts = torch.unique(
        all_nodes, return_inverse=True, return_counts=True
    )
    # Map global node indices to local ones.
    self._assoc[n_id] = torch.arange(n_id.size(0), device=self._device)

    # For each node, compute its temporal embedding:
    # (1) Infer the timestamps of the first event each node participate in
    #     either as a source node or as a (sampled negative) target node.
    _, idx_sorted = torch.sort(n_idx, stable=True)
    cum_sum = n_counts.cumsum(0)
    cum_sum = torch.cat((torch.tensor([0], device=self._device), cum_sum[:-1]))
    first_indices = idx_sorted[cum_sum]
    assert data.t is not None
    # Each node may participate in multiple events and appear (multiple times)
    # as a source node, as a target node, or as a sampled negative target node.
    all_times = data.t.repeat(3)
    node_first_event_timestamps = all_times[first_indices]

    # (2) Look up updated memory embeddings and last updated timestamps of all
    #     nodes involved in the computation.
    memory_embeddings, last_updated_timestamps = self._memory(n_id)
    # (3) Compute the differences between the last updated timestamps and the
    #     inferred first-event timestamps and, finally, compute node embeddings
    #     by shifting the current memory embeddings by element-wise products
    #     between the memory embeddings and the computed differences projected
    #     with a 0-mean Gaussian linear layer. For more details, refer to
    #     Section 3.2 of the original paper:
    #     https://cs.stanford.edu/~srijan/pubs/jodie-kdd2019.pdf; see also
    #     google_research/fm4tlp/modules/emb_module.py?q=symbol:TimeEmbedding).
    node_embeddings = self._gnn(
        memory_embeddings,
        last_updated_timestamps,
        node_first_event_timestamps,
    )

    y_pred_pos = self._link_pred(
        node_embeddings[self._assoc[source_nodes]],
        node_embeddings[self._assoc[target_nodes_pos]],
    )
    y_pred_neg = None
    if target_nodes_neg is not None:
      y_pred_neg = self._link_pred(
          node_embeddings[self._assoc[source_nodes]],
          node_embeddings[self._assoc[target_nodes_neg]],
      )

    return model_template.ModelPrediction(
        y_pred_pos=y_pred_pos,
        y_pred_neg=y_pred_neg,
    )

  def get_memory_embeddings(self, n_id):
    """Gets memory embeddings for a set of nodes."""
    z, unused_last_update = self._memory(n_id)
    return z[0]

  def predict_memory_embeddings(
      self, structral_feat
  ):
    """Predicts memory embeddings from structural embeddings."""
    if not self.has_struct_mapper:
      raise RuntimeError("Struct mapper is not available.")
    return self._struct_mapper(structral_feat)

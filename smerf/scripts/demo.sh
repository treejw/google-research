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

#!/bin/bash
#
# Trains SMERF on a single mipnerf360 scene with a single RTX 3080 Ti 12GB.
#

TIMESTAMP="$(date +'%Y%m%d_%H%M')"
CHECKPOINT_DIR="checkpoints/${TIMESTAMP}-demo"

python3 -m smerf.train \
  --gin_configs=configs/models/smerf.gin \
  --gin_configs=configs/mipnerf360/bicycle.gin \
  --gin_configs=configs/mipnerf360/extras.gin \
  --gin_configs=configs/mipnerf360/rtx3080ti.gin \
  --gin_bindings="smerf.internal.configs.Config.checkpoint_dir = '${CHECKPOINT_DIR}'" \
  --alsologtostderr

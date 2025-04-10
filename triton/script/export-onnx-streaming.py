#!/usr/bin/env python3
#
# Copyright 2023 Xiaomi Corporation (Author: Fangjun Kuang, Wei Kang)
# Copyright 2023 Danqing Fu (danqing.fu@gmail.com)

"""
This script exports a transducer model from PyTorch to ONNX.

We use the pre-trained model from
https://huggingface.co/Zengwei/icefall-asr-librispeech-streaming-zipformer-2023-05-17
as an example to show how to use this file.

1. Download the pre-trained model

cd egs/librispeech/ASR

repo_url=https://huggingface.co/Zengwei/icefall-asr-librispeech-streaming-zipformer-2023-05-17
GIT_LFS_SKIP_SMUDGE=1 git clone $repo_url
repo=$(basename $repo_url)

pushd $repo
git lfs pull --include "exp/pretrained.pt"

cd exp
ln -s pretrained.pt epoch-99.pt
popd

2. Export the model to ONNX

./zipformer/export-onnx-streaming.py \
  --tokens $repo/data/lang_bpe_500/tokens.txt \
  --use-averaged-model 0 \
  --epoch 99 \
  --avg 1 \
  --exp-dir $repo/exp \
  --num-encoder-layers "2,2,3,4,3,2" \
  --downsampling-factor "1,2,4,8,4,2" \
  --feedforward-dim "512,768,1024,1536,1024,768" \
  --num-heads "4,4,4,8,4,4" \
  --encoder-dim "192,256,384,512,384,256" \
  --query-head-dim 32 \
  --value-head-dim 12 \
  --pos-head-dim 4 \
  --pos-dim 48 \
  --encoder-unmasked-dim "192,192,256,256,256,192" \
  --cnn-module-kernel "31,31,15,15,15,31" \
  --decoder-dim 512 \
  --joiner-dim 512 \
  --causal True \
  --chunk-size 16 \
  --left-context-frames 64

The --chunk-size in training is "16,32,64,-1", so we select one of them
(excluding -1) during streaming export. The same applies to `--left-context`,
whose value is "64,128,256,-1".

It will generate the following 3 files inside $repo/exp:

  - encoder-epoch-99-avg-1-chunk-16-left-64.onnx
  - decoder-epoch-99-avg-1-chunk-16-left-64.onnx
  - joiner-epoch-99-avg-1-chunk-16-left-64.onnx

See ./onnx_pretrained-streaming.py for how to use the exported ONNX models.
"""

import argparse
import logging
from pathlib import Path
from typing import Dict, List, Tuple

import k2
import onnx
import torch
import torch.nn as nn
from decoder import Decoder
from onnxruntime.quantization import QuantType, quantize_dynamic
from scaling_converter import convert_scaled_to_non_scaled
from train import add_model_arguments, get_model, get_params
from zipformer import Zipformer2

from icefall.checkpoint import (
    average_checkpoints,
    average_checkpoints_with_averaged_model,
    find_checkpoints,
    load_checkpoint,
)
from icefall.utils import num_tokens, str2bool


def get_parser():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )

    parser.add_argument(
        "--epoch",
        type=int,
        default=28,
        help="""It specifies the checkpoint to use for averaging.
        Note: Epoch counts from 0.
        You can specify --avg to use more checkpoints for model averaging.""",
    )

    parser.add_argument(
        "--iter",
        type=int,
        default=0,
        help="""If positive, --epoch is ignored and it
        will use the checkpoint exp_dir/checkpoint-iter.pt.
        You can specify --avg to use more checkpoints for model averaging.
        """,
    )

    parser.add_argument(
        "--avg",
        type=int,
        default=15,
        help="Number of checkpoints to average. Automatically select "
        "consecutive checkpoints before the checkpoint specified by "
        "'--epoch' and '--iter'",
    )

    parser.add_argument(
        "--use-averaged-model",
        type=str2bool,
        default=True,
        help="Whether to load averaged model. Currently it only supports "
        "using --epoch. If True, it would decode with the averaged model "
        "over the epoch range from `epoch-avg` (excluded) to `epoch`."
        "Actually only the models with epoch number of `epoch-avg` and "
        "`epoch` are loaded for averaging. ",
    )

    parser.add_argument(
        "--exp-dir",
        type=str,
        default="zipformer/exp",
        help="""It specifies the directory where all training related
        files, e.g., checkpoints, log, etc, are saved
        """,
    )

    parser.add_argument(
        "--tokens",
        type=str,
        default="data/lang_bpe_500/tokens.txt",
        help="Path to the tokens.txt",
    )

    parser.add_argument(
        "--context-size",
        type=int,
        default=2,
        help="The context size in the decoder. 1 means bigram; 2 means tri-gram",
    )

    add_model_arguments(parser)

    return parser


def add_meta_data(filename: str, meta_data: Dict[str, str]):
    """Add meta data to an ONNX model. It is changed in-place.

    Args:
      filename:
        Filename of the ONNX model to be changed.
      meta_data:
        Key-value pairs.
    """
    model = onnx.load(filename)
    for key, value in meta_data.items():
        meta = model.metadata_props.add()
        meta.key = key
        meta.value = value

    onnx.save(model, filename)


class OnnxEncoder(nn.Module):
    """A wrapper for Zipformer and the encoder_proj from the joiner"""

    def __init__(
        self, encoder: Zipformer2, encoder_embed: nn.Module, encoder_proj: nn.Linear
    ):
        """
        Args:
          encoder:
            A Zipformer encoder.
          encoder_proj:
            The projection layer for encoder from the joiner.
        """
        super().__init__()
        self.encoder = encoder
        self.encoder_embed = encoder_embed
        self.encoder_proj = encoder_proj
        self.chunk_size = encoder.chunk_size[0]
        self.left_context_len = encoder.left_context_frames[0]
        self.pad_length = 7 + 2 * 3

    def forward(
        self,
        x: torch.Tensor,
        cached_att: torch.Tensor,
        cached_cnn: torch.Tensor,
        cached_embed: torch.Tensor,
        processed_lens: torch.Tensor,
    ) -> Tuple[
        torch.Tensor,
        torch.Tensor,
        torch.Tensor,
        torch.Tensor,
        torch.Tensor,
        torch.Tensor
        ]:
        """
        cached_att:
            The cached key tensors of the first attention modules.
            The cached value tensors of the first attention modules.
            The cached value tensors of the second attention modules.
        cached_cnn:
            The cached left contexts of the first convolution modules.
            The cached left contexts of the second convolution modules.
        cached_embed:
            The cached embeddings of the encoder.
        processed_lens: 
            The processed lengths of utterance.
        """
        N = x.size(0)
        T = self.chunk_size * 2 + self.pad_length
        x_lens = torch.tensor([T] * N, device=x.device)
        left_context_len = self.left_context_len

        x, x_lens, new_cached_embed_left_pad = self.encoder_embed.streaming_forward(
            x=x,
            x_lens=x_lens,
            cached_left_pad=cached_embed,
        )
        assert x.size(1) == self.chunk_size, (x.size(1), self.chunk_size)

        src_key_padding_mask = torch.zeros(N, self.chunk_size, dtype=torch.bool)

        # processed_mask is used to mask out initial states
        processed_mask = torch.arange(left_context_len, device=x.device).expand(
            x.size(0), left_context_len
        )

        # (batch, left_context_size)
        processed_mask = (processed_lens <= processed_mask).flip(1)
        # Update processed lengths
        new_processed_lens = processed_lens + x_lens.unsqueeze(1)
        # (batch, left_context_size + chunk_size)
        src_key_padding_mask = torch.cat([processed_mask, src_key_padding_mask], dim=1)

        x = x.permute(1, 0, 2)
        
        encoder_state = []
        index_att = 0
        index_cnn = 0
        for i, module in enumerate(self.encoder.encoders):
            num_layers = module.num_layers
            embed_dim = self.encoder.encoder_dim[i]
            ds = self.encoder.downsampling_factor[i]
            num_heads = self.encoder.num_heads[i]
            key_dim = self.encoder.query_head_dim[i] * num_heads
            value_dim = self.encoder.value_head_dim[i] * num_heads
            downsample_left = self.encoder.left_context_frames[0] // ds
            nonlin_attn_head_dim = 3 * embed_dim // 4
            conv_left_pad = self.encoder.cnn_module_kernel[i] // 2 
            for layer in range(num_layers):
                cached_key = cached_att[:,index_att:index_att+downsample_left*key_dim].reshape(
                    N, downsample_left, key_dim
                ).transpose(0,1)
                logging.info(f"cached_key_{i}: {cached_key.shape}")
                index_att = index_att + downsample_left*key_dim

                cached_nonlin_attn = cached_att[:,index_att:index_att+downsample_left*nonlin_attn_head_dim].reshape(
                    N,1,downsample_left,nonlin_attn_head_dim
                ).transpose(0,1)
                logging.info(f"cached_nonlin_attn_{i}: {cached_nonlin_attn.shape}")
                index_att = index_att + downsample_left*nonlin_attn_head_dim

                cached_val1 = cached_att[:,index_att:index_att+downsample_left*value_dim].reshape(
                    N, downsample_left, value_dim
                ).transpose(0,1)
                logging.info(f"cached_val1_{i}: {cached_val1.shape}")
                index_att = index_att + downsample_left*value_dim

                cached_val2 = cached_att[:,index_att:index_att+downsample_left*value_dim].reshape(
                    N, downsample_left, value_dim
                ).transpose(0,1)
                logging.info(f"cached_val2_{i}: {cached_val2.shape}")
                index_att = index_att + downsample_left*value_dim

                cached_conv1 = cached_cnn[:,index_cnn:index_cnn+embed_dim*conv_left_pad].reshape(
                    N, embed_dim, conv_left_pad
                )
                logging.info(f"cached_conv1_{i}: {cached_conv1.shape}")
                index_cnn = index_cnn + embed_dim*conv_left_pad

                cached_conv2 = cached_cnn[:,index_cnn:index_cnn+embed_dim*conv_left_pad].reshape(
                    N, embed_dim, conv_left_pad
                )
                logging.info(f"cached_conv2_{i}: {cached_conv2.shape}")
                index_cnn = index_cnn + embed_dim*conv_left_pad

                encoder_state += [
                    cached_key,
                    cached_nonlin_attn,
                    cached_val1,
                    cached_val2,
                    cached_conv1,
                    cached_conv2,
                ]

        (
            encoder_out,
            encoder_out_lens,
            new_encoder_states,
        ) = self.encoder.streaming_forward(
            x=x,
            x_lens=x_lens,
            states=encoder_state,
            src_key_padding_mask=src_key_padding_mask,
        )
        encoder_out = encoder_out.permute(1, 0, 2)
        encoder_out = self.encoder_proj(encoder_out)
        # Now encoder_out is of shape (N, T, joiner_dim)
        new_cached_att = []
        new_cached_cnn = []
        for i, tensor in enumerate(new_encoder_states):
            if (i % 6 == 0) or (i % 6 == 1) or (i % 6 == 2) or (i % 6 == 3):
                new_cached_att.append(tensor.transpose(0,1).reshape(N, -1))
            elif (i % 6 == 4) or (i % 6 == 5):
                new_cached_cnn.append(tensor.reshape(N, -1))

        new_cached_att = torch.cat(new_cached_att, dim=1)
        new_cached_cnn = torch.cat(new_cached_cnn, dim=1)            

        return (encoder_out, 
                new_cached_att, 
                new_cached_cnn, 
                new_cached_embed_left_pad, 
                new_processed_lens)

    def get_init_states(
        self,
        batch_size: int = 1,
        device: torch.device = torch.device("cpu"),
    ) -> List[torch.Tensor]:
        """
        Returns a list of cached tensors of all encoder layers. For layer-i, states[i*6:(i+1)*6]
        is (cached_key, cached_nonlin_attn, cached_val1, cached_val2, cached_conv1, cached_conv2).
        states[-2] is the cached left padding for ConvNeXt module,
        of shape (batch_size, num_channels, left_pad, num_freqs)
        states[-1] is processed_lens of shape (batch,), which records the number
        of processed frames (at 50hz frame rate, after encoder_embed) for each sample in batch.
        """
        states = self.encoder.get_init_states(batch_size, device)
        
        initial_cached_att = []
        initial_cached_cnn = []
        for i, tensor in enumerate(states):
            if (i % 6 == 0) or (i % 6 == 1) or (i % 6 == 2) or (i % 6 == 3):
                initial_cached_att.append(tensor.transpose(0,1).reshape(batch_size, -1))
            elif (i % 6 == 4) or (i % 6 == 5):
                initial_cached_cnn.append(tensor.reshape(batch_size, -1))
        initial_cached_att = torch.cat(initial_cached_att, dim=1)
        initial_cached_cnn = torch.cat(initial_cached_cnn, dim=1)   
            
        embed_states = self.encoder_embed.get_init_states(batch_size, device)

        processed_lens = torch.zeros(batch_size,1, dtype=torch.int64, device=device)

        return initial_cached_att, initial_cached_cnn, embed_states, processed_lens


class OnnxDecoder(nn.Module):
    """A wrapper for Decoder and the decoder_proj from the joiner"""

    def __init__(self, decoder: Decoder, decoder_proj: nn.Linear):
        super().__init__()
        self.decoder = decoder
        self.decoder_proj = decoder_proj

    def forward(self, y: torch.Tensor) -> torch.Tensor:
        """
        Args:
          y:
            A 2-D tensor of shape (N, context_size).
        Returns
          Return a 2-D tensor of shape (N, joiner_dim)
        """
        need_pad = False
        decoder_output = self.decoder(y, need_pad=need_pad)
        decoder_output = decoder_output.squeeze(1)
        output = self.decoder_proj(decoder_output)

        return output


class OnnxJoiner(nn.Module):
    """A wrapper for the joiner"""

    def __init__(self, output_linear: nn.Linear):
        super().__init__()
        self.output_linear = output_linear

    def forward(
        self,
        encoder_out: torch.Tensor,
        decoder_out: torch.Tensor,
    ) -> torch.Tensor:
        """
        Args:
          encoder_out:
            A 2-D tensor of shape (N, joiner_dim)
          decoder_out:
            A 2-D tensor of shape (N, joiner_dim)
        Returns:
          Return a 2-D tensor of shape (N, vocab_size)
        """
        logit = encoder_out + decoder_out
        logit = self.output_linear(torch.tanh(logit))
        return logit


def export_encoder_model_onnx(
    encoder_model: OnnxEncoder,
    encoder_filename: str,
    opset_version: int = 11,
) -> None:
    encoder_model.encoder.__class__.forward = (
        encoder_model.encoder.__class__.streaming_forward
    )

    decode_chunk_len = encoder_model.chunk_size * 2
    # The encoder_embed subsample features (T - 7) // 2
    # The ConvNeXt module needs (7 - 1) // 2 = 3 frames of right padding after subsampling
    T = decode_chunk_len + encoder_model.pad_length

    x = torch.rand(1, T, 80, dtype=torch.float32)
    init_state = encoder_model.get_init_states()
    num_encoders = len(encoder_model.encoder.encoder_dim)
    logging.info(f"num_encoders: {num_encoders}")

    inputs = {}
    input_names = ["x"]

    outputs = {}
    output_names = ["encoder_out"]

    num_encoder_layers = ",".join(map(str, encoder_model.encoder.num_encoder_layers))
    encoder_dims = ",".join(map(str, encoder_model.encoder.encoder_dim))
    cnn_module_kernels = ",".join(map(str, encoder_model.encoder.cnn_module_kernel))
    ds = encoder_model.encoder.downsampling_factor
    left_context_len = encoder_model.left_context_len
    left_context_len = [left_context_len // k for k in ds]
    left_context_len = ",".join(map(str, left_context_len))
    query_head_dims = ",".join(map(str, encoder_model.encoder.query_head_dim))
    value_head_dims = ",".join(map(str, encoder_model.encoder.value_head_dim))
    num_heads = ",".join(map(str, encoder_model.encoder.num_heads))

    meta_data = {
        "model_type": "zipformer2",
        "version": "1",
        "model_author": "k2-fsa",
        "comment": "streaming zipformer2",
        "decode_chunk_len": str(decode_chunk_len),  # 32
        "T": str(T),  # 32+7+2*3=45
        "num_encoder_layers": num_encoder_layers,
        "encoder_dims": encoder_dims,
        "cnn_module_kernels": cnn_module_kernels,
        "left_context_len": left_context_len,
        "query_head_dims": query_head_dims,
        "value_head_dims": value_head_dims,
        "num_heads": num_heads,
    }
    logging.info(f"meta_data: {meta_data}")

     # attention cached
    cached_att = init_state[0]
    name = "cached_att"
    logging.info(f"{name}.shape: {cached_att.shape}")
    inputs[name] = {0: "N"}
    outputs[f"new_{name}"] = {0: "N"}
    input_names.append(name)
    output_names.append(f"new_{name}")

    # CNN cached
    cached_cnn = init_state[1]
    name = "cached_cnn"
    logging.info(f"{name}.shape: {cached_cnn.shape}")
    inputs[name] = {0: "N"}
    outputs[f"new_{name}"] = {0: "N"}
    input_names.append(name)
    output_names.append(f"new_{name}")

    # (batch_size, channels, left_pad, freq)
    embed_states = init_state[-2]
    name = "embed_states"
    logging.info(f"{name}.shape: {embed_states.shape}")
    inputs[name] = {0: "N"}
    outputs[f"new_{name}"] = {0: "N"}
    input_names.append(name)
    output_names.append(f"new_{name}")

    # (batch_size,)
    processed_lens = init_state[-1]
    name = "processed_lens"
    logging.info(f"{name}.shape: {processed_lens.shape}")
    inputs[name] = {0: "N"}
    outputs[f"new_{name}"] = {0: "N"}
    input_names.append(name)
    output_names.append(f"new_{name}")

    logging.info(inputs)
    logging.info(outputs)
    logging.info(input_names)
    logging.info(output_names)

    torch.onnx.export(
        encoder_model,
        (x, cached_att, cached_cnn, embed_states, processed_lens),
        encoder_filename,
        verbose=False,
        opset_version=opset_version,
        input_names=[
            "x",
            "cached_att",
            "cached_cnn",
            "embed_states",
            "processed_lens",
        ],
        output_names=[
            "encoder_out",
            "new_cached_att",
            "new_cached_cnn",
            "new_embed_states",
            "new_processed_lens",
        ],
        dynamic_axes={
            "x": {0: "N", 1: "T"},
            "encoder_out": {0: "N"},
            **inputs,
            **outputs,
        },
    )

    add_meta_data(filename=encoder_filename, meta_data=meta_data)


def export_decoder_model_onnx(
    decoder_model: OnnxDecoder,
    decoder_filename: str,
    opset_version: int = 11,
) -> None:
    """Export the decoder model to ONNX format.

    The exported model has one input:

        - y: a torch.int64 tensor of shape (N, decoder_model.context_size)

    and has one output:

        - decoder_out: a torch.float32 tensor of shape (N, joiner_dim)

    Args:
      decoder_model:
        The decoder model to be exported.
      decoder_filename:
        Filename to save the exported ONNX model.
      opset_version:
        The opset version to use.
    """
    context_size = decoder_model.decoder.context_size
    vocab_size = decoder_model.decoder.vocab_size

    y = torch.zeros(10, context_size, dtype=torch.int64)
    decoder_model = torch.jit.script(decoder_model)
    torch.onnx.export(
        decoder_model,
        y,
        decoder_filename,
        verbose=False,
        opset_version=opset_version,
        input_names=["y"],
        output_names=["decoder_out"],
        dynamic_axes={
            "y": {0: "N"},
            "decoder_out": {0: "N"},
        },
    )

    meta_data = {
        "context_size": str(context_size),
        "vocab_size": str(vocab_size),
    }
    add_meta_data(filename=decoder_filename, meta_data=meta_data)


def export_joiner_model_onnx(
    joiner_model: nn.Module,
    joiner_filename: str,
    opset_version: int = 11,
) -> None:
    """Export the joiner model to ONNX format.
    The exported joiner model has two inputs:

        - encoder_out: a tensor of shape (N, joiner_dim)
        - decoder_out: a tensor of shape (N, joiner_dim)

    and produces one output:

        - logit: a tensor of shape (N, vocab_size)
    """
    joiner_dim = joiner_model.output_linear.weight.shape[1]
    logging.info(f"joiner dim: {joiner_dim}")

    projected_encoder_out = torch.rand(11, joiner_dim, dtype=torch.float32)
    projected_decoder_out = torch.rand(11, joiner_dim, dtype=torch.float32)

    torch.onnx.export(
        joiner_model,
        (projected_encoder_out, projected_decoder_out),
        joiner_filename,
        verbose=False,
        opset_version=opset_version,
        input_names=[
            "encoder_out",
            "decoder_out",
        ],
        output_names=["logit"],
        dynamic_axes={
            "encoder_out": {0: "N"},
            "decoder_out": {0: "N"},
            "logit": {0: "N"},
        },
    )
    meta_data = {
        "joiner_dim": str(joiner_dim),
    }
    add_meta_data(filename=joiner_filename, meta_data=meta_data)


@torch.no_grad()
def main():
    args = get_parser().parse_args()
    args.exp_dir = Path(args.exp_dir)

    params = get_params()
    params.update(vars(args))

    device = torch.device("cpu")
    if torch.cuda.is_available():
        device = torch.device("cuda", 0)

    logging.info(f"device: {device}")

    token_table = k2.SymbolTable.from_file(params.tokens)
    params.blank_id = token_table["<blk>"]
    params.vocab_size = num_tokens(token_table) + 1

    logging.info(params)

    logging.info("About to create model")
    model = get_model(params)

    model.to(device)

    if not params.use_averaged_model:
        if params.iter > 0:
            filenames = find_checkpoints(params.exp_dir, iteration=-params.iter)[
                : params.avg
            ]
            if len(filenames) == 0:
                raise ValueError(
                    f"No checkpoints found for"
                    f" --iter {params.iter}, --avg {params.avg}"
                )
            elif len(filenames) < params.avg:
                raise ValueError(
                    f"Not enough checkpoints ({len(filenames)}) found for"
                    f" --iter {params.iter}, --avg {params.avg}"
                )
            logging.info(f"averaging {filenames}")
            model.to(device)
            model.load_state_dict(
                average_checkpoints(filenames, device=device), strict=False
            )
        elif params.avg == 1:
            load_checkpoint(f"{params.exp_dir}/epoch-{params.epoch}.pt", model)
        else:
            start = params.epoch - params.avg + 1
            filenames = []
            for i in range(start, params.epoch + 1):
                if i >= 1:
                    filenames.append(f"{params.exp_dir}/epoch-{i}.pt")
            logging.info(f"averaging {filenames}")
            model.to(device)
            model.load_state_dict(
                average_checkpoints(filenames, device=device), strict=False
            )
    else:
        if params.iter > 0:
            filenames = find_checkpoints(params.exp_dir, iteration=-params.iter)[
                : params.avg + 1
            ]
            if len(filenames) == 0:
                raise ValueError(
                    f"No checkpoints found for"
                    f" --iter {params.iter}, --avg {params.avg}"
                )
            elif len(filenames) < params.avg + 1:
                raise ValueError(
                    f"Not enough checkpoints ({len(filenames)}) found for"
                    f" --iter {params.iter}, --avg {params.avg}"
                )
            filename_start = filenames[-1]
            filename_end = filenames[0]
            logging.info(
                "Calculating the averaged model over iteration checkpoints"
                f" from {filename_start} (excluded) to {filename_end}"
            )
            model.to(device)
            model.load_state_dict(
                average_checkpoints_with_averaged_model(
                    filename_start=filename_start,
                    filename_end=filename_end,
                    device=device,
                ),
                strict=False,
            )
        else:
            assert params.avg > 0, params.avg
            start = params.epoch - params.avg
            assert start >= 1, start
            filename_start = f"{params.exp_dir}/epoch-{start}.pt"
            filename_end = f"{params.exp_dir}/epoch-{params.epoch}.pt"
            logging.info(
                f"Calculating the averaged model over epoch range from "
                f"{start} (excluded) to {params.epoch}"
            )
            model.to(device)
            model.load_state_dict(
                average_checkpoints_with_averaged_model(
                    filename_start=filename_start,
                    filename_end=filename_end,
                    device=device,
                ),
                strict=False,
            )

    model.to("cpu")
    model.eval()

    convert_scaled_to_non_scaled(model, inplace=True)

    encoder = OnnxEncoder(
        encoder=model.encoder,
        encoder_embed=model.encoder_embed,
        encoder_proj=model.joiner.encoder_proj,
    )

    decoder = OnnxDecoder(
        decoder=model.decoder,
        decoder_proj=model.joiner.decoder_proj,
    )

    joiner = OnnxJoiner(output_linear=model.joiner.output_linear)

    encoder_num_param = sum([p.numel() for p in encoder.parameters()])
    decoder_num_param = sum([p.numel() for p in decoder.parameters()])
    joiner_num_param = sum([p.numel() for p in joiner.parameters()])
    total_num_param = encoder_num_param + decoder_num_param + joiner_num_param
    logging.info(f"encoder parameters: {encoder_num_param}")
    logging.info(f"decoder parameters: {decoder_num_param}")
    logging.info(f"joiner parameters: {joiner_num_param}")
    logging.info(f"total parameters: {total_num_param}")

    if params.iter > 0:
        suffix = f"iter-{params.iter}"
    else:
        suffix = f"epoch-{params.epoch}"

    suffix += f"-avg-{params.avg}"
    suffix += f"-chunk-{params.chunk_size}"
    suffix += f"-left-{params.left_context_frames}"

    opset_version = 13

    logging.info("Exporting encoder")
    encoder_filename = params.exp_dir / f"encoder-{suffix}.onnx"
    export_encoder_model_onnx(
        encoder,
        encoder_filename,
        opset_version=opset_version,
    )
    logging.info(f"Exported encoder to {encoder_filename}")

    logging.info("Exporting decoder")
    decoder_filename = params.exp_dir / f"decoder-{suffix}.onnx"
    export_decoder_model_onnx(
        decoder,
        decoder_filename,
        opset_version=opset_version,
    )
    logging.info(f"Exported decoder to {decoder_filename}")

    logging.info("Exporting joiner")
    joiner_filename = params.exp_dir / f"joiner-{suffix}.onnx"
    export_joiner_model_onnx(
        joiner,
        joiner_filename,
        opset_version=opset_version,
    )
    logging.info(f"Exported joiner to {joiner_filename}")

    # Generate int8 quantization models
    # See https://onnxruntime.ai/docs/performance/model-optimizations/quantization.html#data-type-selection

    logging.info("Generate int8 quantization models")

    encoder_filename_int8 = params.exp_dir / f"encoder-{suffix}.int8.onnx"
    quantize_dynamic(
        model_input=encoder_filename,
        model_output=encoder_filename_int8,
        op_types_to_quantize=["MatMul"],
        weight_type=QuantType.QInt8,
    )

    decoder_filename_int8 = params.exp_dir / f"decoder-{suffix}.int8.onnx"
    quantize_dynamic(
        model_input=decoder_filename,
        model_output=decoder_filename_int8,
        op_types_to_quantize=["MatMul", "Gather"],
        weight_type=QuantType.QInt8,
    )

    joiner_filename_int8 = params.exp_dir / f"joiner-{suffix}.int8.onnx"
    quantize_dynamic(
        model_input=joiner_filename,
        model_output=joiner_filename_int8,
        op_types_to_quantize=["MatMul"],
        weight_type=QuantType.QInt8,
    )


if __name__ == "__main__":
    formatter = "%(asctime)s %(levelname)s [%(filename)s:%(lineno)d] %(message)s"
    logging.basicConfig(format=formatter, level=logging.INFO)
    main()

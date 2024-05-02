# python3 client_copy.py \
#     --manifest-dir /home/quangnv53/sherpa/triton/client/Triton-ASR-Client/datasets/mini_en \
#     --audio-path /home/quangnv53/sherpa/triton/client/Triton-ASR-Client/datasets/mini_en/wav/1089-134686-0001.wav \
#     --model-name transducer \
#     --simulate-streaming \
#     --encoder_right_context 0 \
#     --chunk_size 16 \
#     --subsampling 2 \
#     --num-tasks 100

python3 client_copy.py \
    --manifest-dir /data/quangnv53/data_hub/librispeech/kaldi_manifest/test-clean \
    --model-name transducer \
    --simulate-streaming \
    --encoder_right_context 0 \
    --chunk_size 16 \
    --subsampling 2 \
    --num-tasks 100
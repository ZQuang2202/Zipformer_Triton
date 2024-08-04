python3 client_copy.py \
    --manifest-dir /data_hub/librispeech/kaldi_manifest/test-clean \
    --model-name transducer \
    --simulate-streaming \
    --encoder_right_context 0 \
    --chunk_size 16 \
    --subsampling 2 \
    --num-tasks 100
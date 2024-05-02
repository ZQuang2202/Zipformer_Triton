sudo docker run --gpus 'device=0' --rm -v $PWD:/workspace/sherpa --name sherpa_server --net host --shm-size=1g --ulimit memlock=-1 --ulimit stack=67108864 -it sherpa_triton_server

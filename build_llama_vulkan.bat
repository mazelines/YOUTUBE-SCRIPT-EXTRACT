@echo off
call "C:\Program Files\Microsoft Visual Studio\2022\Professional\VC\Auxiliary\Build\vcvarsall.bat" x64
set CMAKE_GENERATOR=Ninja
set CMAKE_ARGS=-DGGML_NATIVE=OFF -DGGML_AVX2=ON -DGGML_FMA=ON -DGGML_F16C=ON -DGGML_AVX512=OFF -DGGML_VULKAN=ON -DCMAKE_C_FLAGS=/utf-8 -DCMAKE_CXX_FLAGS=/utf-8
pip install "llama-cpp-python>=0.3.0" --no-binary llama-cpp-python --no-cache-dir --force-reinstall

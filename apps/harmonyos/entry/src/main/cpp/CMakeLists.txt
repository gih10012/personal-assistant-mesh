cmake_minimum_required(VERSION 3.5.0)
project(amita_screenshare)

add_library(amita_screenshare SHARED amita_screenshare.cpp)
target_compile_features(amita_screenshare PRIVATE cxx_std_17)
target_compile_options(amita_screenshare PRIVATE -Wall -Wextra)
target_link_libraries(amita_screenshare PUBLIC
    libace_napi.z.so
    libnative_avscreen_capture.so
    libnative_media_core.so
    libnative_buffer.so
    libnative_display_manager.so
    libpixelmap.so
    libimage_packer.so)

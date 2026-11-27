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

# Workspace implementation pinned to harmony-agent-kernel e889325.
add_library(harmony_agent_workspace SHARED kernel/workspace.cpp kernel/workspace_napi.cpp)
target_include_directories(harmony_agent_workspace PRIVATE kernel/include)
target_compile_features(harmony_agent_workspace PRIVATE cxx_std_17)
target_link_libraries(harmony_agent_workspace PRIVATE libace_napi.z.so)

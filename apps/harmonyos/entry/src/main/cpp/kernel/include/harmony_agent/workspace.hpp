#pragma once

#include <cstddef>
#include <string>
#include <string_view>

namespace harmony_agent {

inline constexpr std::size_t workspace_file_limit = 16 * 1024 * 1024;

std::string read_workspace_file(const std::string &root, std::string_view path);
void write_workspace_file(const std::string &root, std::string_view path, std::string_view contents);

}

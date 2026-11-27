#include <napi/native_api.h>

#include "harmony_agent/workspace.hpp"

#include <cstring>
#include <stdexcept>
#include <vector>

namespace {

std::string path_argument(napi_env env, napi_value value) {
    size_t length = 0;
    if (napi_get_value_string_utf8(env, value, nullptr, 0, &length) != napi_ok || length == 0 || length > 4096) {
        throw std::invalid_argument("workspace path invalid");
    }
    std::vector<char> buffer(length + 1);
    size_t written = 0;
    if (napi_get_value_string_utf8(env, value, buffer.data(), buffer.size(), &written) != napi_ok || written != length) {
        throw std::invalid_argument("workspace path invalid");
    }
    return std::string(buffer.data(), written);
}

napi_value Read(napi_env env, napi_callback_info info) {
    try {
        size_t argc = 2;
        napi_value args[2]{};
        if (napi_get_cb_info(env, info, &argc, args, nullptr, nullptr) != napi_ok || argc != 2) {
            throw std::invalid_argument("expected workspace root and path");
        }
        const auto contents = harmony_agent::read_workspace_file(path_argument(env, args[0]), path_argument(env, args[1]));
        void *buffer = nullptr;
        napi_value value = nullptr;
        if (napi_create_arraybuffer(env, contents.size(), &buffer, &value) != napi_ok) {
            throw std::runtime_error("workspace buffer unavailable");
        }
        if (!contents.empty()) std::memcpy(buffer, contents.data(), contents.size());
        return value;
    } catch (const std::exception &) {
        napi_throw_error(env, "WORKSPACE_READ_FAILED", "Workspace file could not be read");
        return nullptr;
    }
}

napi_value Write(napi_env env, napi_callback_info info) {
    try {
        size_t argc = 3;
        napi_value args[3]{};
        if (napi_get_cb_info(env, info, &argc, args, nullptr, nullptr) != napi_ok || argc != 3) {
            throw std::invalid_argument("expected workspace root, path, and bytes");
        }
        void *buffer = nullptr;
        size_t bytes = 0;
        if (napi_get_arraybuffer_info(env, args[2], &buffer, &bytes) != napi_ok ||
                bytes > harmony_agent::workspace_file_limit) {
            throw std::invalid_argument("workspace bytes invalid");
        }
        const auto data = bytes == 0 ? std::string_view() : std::string_view(static_cast<const char *>(buffer), bytes);
        harmony_agent::write_workspace_file(path_argument(env, args[0]), path_argument(env, args[1]), data);
        napi_value result = nullptr;
        napi_get_undefined(env, &result);
        return result;
    } catch (const std::exception &) {
        napi_throw_error(env, "WORKSPACE_WRITE_UNCONFIRMED", "Workspace write did not return a confirmed result");
        return nullptr;
    }
}

napi_value Init(napi_env env, napi_value exports) {
    const napi_property_descriptor methods[] = {
        {"readFile", nullptr, Read, nullptr, nullptr, nullptr, napi_default, nullptr},
        {"writeFile", nullptr, Write, nullptr, nullptr, nullptr, napi_default, nullptr}
    };
    napi_define_properties(env, exports, 2, methods);
    return exports;
}

}

static napi_module module = {1, 0, nullptr, Init, "harmony_agent_workspace", nullptr, {0}};
extern "C" __attribute__((constructor)) void RegisterHarmonyAgentWorkspace() { napi_module_register(&module); }

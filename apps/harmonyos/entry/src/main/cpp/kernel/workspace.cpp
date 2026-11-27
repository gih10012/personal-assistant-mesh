#include "harmony_agent/workspace.hpp"

#include <cerrno>
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>
#include <stdexcept>
#include <system_error>
#include <utility>

namespace {

[[noreturn]] void failed(const char *operation, int error = errno) {
    throw std::system_error(error, std::generic_category(), operation);
}

class Descriptor {
public:
    explicit Descriptor(int fd) : fd_(fd) {
        if (fd < 0) failed("open");
    }
    Descriptor(const Descriptor &) = delete;
    Descriptor &operator=(const Descriptor &) = delete;
    Descriptor(Descriptor &&other) noexcept : fd_(std::exchange(other.fd_, -1)) {}
    Descriptor &operator=(Descriptor &&other) noexcept {
        if (this != &other) {
            if (fd_ >= 0) close(fd_);
            fd_ = std::exchange(other.fd_, -1);
        }
        return *this;
    }
    ~Descriptor() { if (fd_ >= 0) close(fd_); }
    int get() const { return fd_; }

private:
    int fd_;
};

struct WorkspaceFile {
    Descriptor parent;
    std::string leaf;
};

WorkspaceFile resolve_file(const Descriptor &root, std::string_view relative) {
    Descriptor directory(openat(root.get(), ".", O_RDONLY | O_DIRECTORY | O_CLOEXEC));
    for (;;) {
        const auto separator = relative.find('/');
        const std::string part(relative.substr(0, separator));
        if (part.empty() || part == "." || part == ".." || part.find('\0') != std::string::npos) {
            throw std::invalid_argument("expected a relative workspace path");
        }
        if (separator == std::string_view::npos) return {std::move(directory), part};
        directory = Descriptor(openat(directory.get(), part.c_str(), O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC));
        relative.remove_prefix(separator + 1);
    }
}

void write_all(int output, std::string_view data) {
    while (!data.empty()) {
        const auto written = write(output, data.data(), data.size());
        if (written < 0 && errno == EINTR) continue;
        if (written < 0) failed("write");
        if (written == 0) failed("write", EIO);
        data.remove_prefix(static_cast<std::size_t>(written));
    }
}

std::string read_file(const WorkspaceFile &file) {
    Descriptor source(openat(file.parent.get(), file.leaf.c_str(), O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK));
    struct stat info{};
    if (fstat(source.get(), &info) != 0) failed("fstat");
    if (!S_ISREG(info.st_mode)) throw std::invalid_argument("workspace input must be a regular file");
    if (info.st_size > static_cast<off_t>(harmony_agent::workspace_file_limit)) throw std::runtime_error("file is larger than 16 MiB");
    std::string contents;
    char buffer[16384];
    for (;;) {
        const auto count = read(source.get(), buffer, sizeof(buffer));
        if (count == 0) return contents;
        if (count < 0) {
            if (errno == EINTR) continue;
            failed("read");
        }
        if (contents.size() + count > harmony_agent::workspace_file_limit) {
            throw std::runtime_error("file is larger than 16 MiB");
        }
        contents.append(buffer, static_cast<std::size_t>(count));
    }
}

void write_file(const WorkspaceFile &file, std::string_view contents) {
    const int parent = file.parent.get();
    struct stat current{};
    if (fstatat(parent, file.leaf.c_str(), &current, AT_SYMLINK_NOFOLLOW) == 0) {
        if (!S_ISREG(current.st_mode)) throw std::invalid_argument("workspace output must be a regular file");
    } else if (errno != ENOENT) {
        failed("fstatat");
    }
    const std::string temporary = ".harmony-agent-write-" + std::to_string(getpid());
    Descriptor output(openat(parent, temporary.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600));
    try {
        write_all(output.get(), contents);
        if (fsync(output.get()) != 0) failed("file fsync");
        if (renameat(parent, temporary.c_str(), parent, file.leaf.c_str()) != 0) failed("renameat");
        if (fsync(parent) != 0) failed("directory fsync after rename");
    } catch (...) {
        unlinkat(parent, temporary.c_str(), 0);
        throw;
    }
}


Descriptor workspace_root(const std::string &root) {
    if (root.empty() || root.front() != '/' || root.find('\0') != std::string::npos) {
        throw std::invalid_argument("expected an absolute workspace");
    }
    return Descriptor(open(root.c_str(), O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC));
}

}  // namespace

std::string harmony_agent::read_workspace_file(const std::string &root, std::string_view path) {
    return read_file(resolve_file(workspace_root(root), path));
}

void harmony_agent::write_workspace_file(const std::string &root, std::string_view path, std::string_view contents) {
    if (contents.size() > workspace_file_limit) throw std::runtime_error("file is larger than 16 MiB");
    write_file(resolve_file(workspace_root(root), path), contents);
}

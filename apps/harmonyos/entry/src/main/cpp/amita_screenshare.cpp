// SPDX-License-Identifier: Apache-2.0
// AMITA: public, owner-authorized video-only screen capture. No disk, sockets or input injection.
#include <napi/native_api.h>
#include <multimedia/player_framework/native_avscreen_capture.h>
#include <multimedia/player_framework/native_avbuffer.h>
#include <multimedia/image_framework/image/pixelmap_native.h>
#include <multimedia/image_framework/image/image_packer_native.h>
#include <native_buffer/native_buffer.h>
#include <window_manager/oh_display_manager.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <cmath>
#include <condition_variable>
#include <cstdint>
#include <cstring>
#include <memory>
#include <mutex>
#include <new>
#include <string>
#include <thread>
#include <utility>
#include <vector>

namespace {
constexpr int32_t ERR_DISPLAY = -1002;
constexpr int32_t ERR_FORMAT = -1003;
constexpr int32_t ERR_MAP = -1004;
constexpr int32_t ERR_BOUNDS = -1005;
constexpr int32_t ERR_ENCODE = -1006;
constexpr int32_t ERR_JPEG_LIMIT = -1007;
constexpr int32_t ERR_CALLBACK = -1008;
constexpr int32_t ERR_MEMORY = -1009;
constexpr uint64_t MAX_INPUT_BYTES = 128ULL * 1024 * 1024;
constexpr int32_t MAX_INPUT_DIMENSION = 8192;
constexpr int64_t FRAME_INTERVAL_NS = 200000000; // At most five actual capture frames per second.

struct Options { int32_t maxWidth = 640; int32_t maxHeight = 640; size_t maxBytes = 42000; };
struct Frame {
    std::vector<uint8_t> bytes;
    int32_t width = 0;
    int32_t height = 0;
    int64_t timestampNs = 0;
    uint64_t revision = 0;
};
struct State { const char *state = ""; int32_t errorCode = 0; };
struct FrameBridge {
    std::mutex mutex;
    std::unique_ptr<Frame> latest;
    bool scheduled = false;
    std::atomic<bool> active {true};
};
struct StateBridge {
    std::mutex mutex;
    std::array<State, 16> pending {};
    size_t count = 0;
    bool scheduled = false;
    std::atomic<bool> active {true};
};

void SetNumber(napi_env env, napi_value object, const char *key, double value)
{
    napi_value number = nullptr;
    if (napi_create_double(env, value, &number) == napi_ok) { napi_set_named_property(env, object, key, number); }
}

void CallStateJs(napi_env env, napi_value callback, void *context, void *)
{
    auto *bridge = static_cast<StateBridge *>(context);
    std::array<State, 16> states {};
    size_t count = 0;
    {
        std::lock_guard<std::mutex> lock(bridge->mutex);
        count = bridge->count;
        std::copy_n(bridge->pending.begin(), count, states.begin());
        bridge->count = 0; bridge->scheduled = false;
    }
    if (!env || !callback || !bridge->active.load()) { return; }
    for (size_t index = 0; index < count; index++) {
        const auto &state = states[index];
        napi_value object = nullptr, text = nullptr, receiver = nullptr, result = nullptr;
        if (napi_create_object(env, &object) != napi_ok ||
            napi_create_string_utf8(env, state.state, NAPI_AUTO_LENGTH, &text) != napi_ok) { return; }
        napi_set_named_property(env, object, "state", text);
        SetNumber(env, object, "errorCode", state.errorCode);
        napi_get_undefined(env, &receiver);
        if (napi_call_function(env, receiver, callback, 1, &object, &result) != napi_ok) { return; }
    }
}

void CallFrameJs(napi_env env, napi_value callback, void *context, void *)
{
    auto *bridge = static_cast<FrameBridge *>(context);
    std::unique_ptr<Frame> frame;
    {
        std::lock_guard<std::mutex> lock(bridge->mutex);
        frame = std::move(bridge->latest); bridge->scheduled = false;
    }
    if (!env || !callback || !frame || !bridge->active.load()) { return; }
    napi_value object = nullptr, arrayBuffer = nullptr, bytes = nullptr, receiver = nullptr, result = nullptr;
    void *destination = nullptr;
    if (napi_create_object(env, &object) != napi_ok ||
        napi_create_arraybuffer(env, frame->bytes.size(), &destination, &arrayBuffer) != napi_ok) { return; }
    std::memcpy(destination, frame->bytes.data(), frame->bytes.size());
    if (napi_create_typedarray(env, napi_uint8_array, frame->bytes.size(), arrayBuffer, 0, &bytes) != napi_ok) { return; }
    napi_set_named_property(env, object, "bytes", bytes);
    SetNumber(env, object, "width", frame->width);
    SetNumber(env, object, "height", frame->height);
    SetNumber(env, object, "timestamp", static_cast<double>(frame->timestampNs) / 1000000.0);
    SetNumber(env, object, "revision", static_cast<double>(frame->revision));
    napi_get_undefined(env, &receiver);
    napi_call_function(env, receiver, callback, 1, &object, &result);
}

void DeleteFrameBridge(napi_env, void *data, void *) { delete static_cast<FrameBridge *>(data); }
void DeleteStateBridge(napi_env, void *data, void *) { delete static_cast<StateBridge *>(data); }

// OH_AVBuffer is valid only inside the capture callback. GetNativeBuffer creates a reference
// that must be released; mapping, conversion and all reads finish before that callback returns.
struct MappedBuffer {
    OH_NativeBuffer *buffer = nullptr;
    void *address = nullptr;
    bool mapped = false;
    ~MappedBuffer()
    {
        if (mapped) { OH_NativeBuffer_Unmap(buffer); }
        if (buffer) { OH_NativeBuffer_Unreference(buffer); }
    }
};

bool PlaneFits(const OH_NativeBuffer_Plane &plane, uint32_t width, uint32_t height,
    uint32_t bytesPerPixel, uint64_t capacity)
{
    if (!width || !height || plane.columnStride < bytesPerPixel || !plane.rowStride ||
        plane.offset >= capacity || plane.rowStride > capacity || plane.columnStride > capacity) { return false; }
    const uint64_t rowBytes = static_cast<uint64_t>(width - 1) * plane.columnStride + bytesPerPixel;
    const uint64_t last = plane.offset + static_cast<uint64_t>(height - 1) * plane.rowStride + rowBytes;
    return rowBytes <= plane.rowStride && last <= capacity;
}

uint8_t ClampColor(double value)
{
    return static_cast<uint8_t>(std::max(0.0, std::min(255.0, std::round(value))));
}

int32_t CopyFrame(OH_AVBuffer *avBuffer, int64_t timestamp, const Options &options, Frame &out)
{
    if (!avBuffer || timestamp <= 0) { return ERR_BOUNDS; }
    const int32_t capacityValue = OH_AVBuffer_GetCapacity(avBuffer);
    if (capacityValue <= 0 || static_cast<uint64_t>(capacityValue) > MAX_INPUT_BYTES) { return ERR_BOUNDS; }
    const uint64_t capacity = static_cast<uint64_t>(capacityValue);
    MappedBuffer mapped;
    mapped.buffer = OH_AVBuffer_GetNativeBuffer(avBuffer);
    if (!mapped.buffer) { return ERR_FORMAT; }
    OH_NativeBuffer_Config config {};
    OH_NativeBuffer_GetConfig(mapped.buffer, &config);
    if (config.width <= 0 || config.height <= 0 || config.width > MAX_INPUT_DIMENSION ||
        config.height > MAX_INPUT_DIMENSION) { return ERR_BOUNDS; }
    const bool rgba = config.format == NATIVEBUFFER_PIXEL_FMT_RGBA_8888 ||
        config.format == NATIVEBUFFER_PIXEL_FMT_RGBX_8888;
    const bool bgra = config.format == NATIVEBUFFER_PIXEL_FMT_BGRA_8888 ||
        config.format == NATIVEBUFFER_PIXEL_FMT_BGRX_8888;
    const bool nv12 = config.format == NATIVEBUFFER_PIXEL_FMT_YCBCR_420_SP;
    const bool nv21 = config.format == NATIVEBUFFER_PIXEL_FMT_YCRCB_420_SP;
    if (!rgba && !bgra && !nv12 && !nv21) { return ERR_FORMAT; }

    OH_NativeBuffer_Planes planes {};
    int32_t mapResult = OH_NativeBuffer_MapPlanes(mapped.buffer, &mapped.address, &planes);
    if (mapResult == 0 && mapped.address) { mapped.mapped = true; }
    else if (rgba || bgra) {
        if (OH_NativeBuffer_Map(mapped.buffer, &mapped.address) != 0 || !mapped.address) { return ERR_MAP; }
        mapped.mapped = true;
        planes.planeCount = 1;
        planes.planes[0] = {0, static_cast<uint32_t>(std::max(0, config.stride)), 4};
    } else { return ERR_MAP; }
    if (planes.planeCount < 1 || planes.planeCount > 4) { return ERR_BOUNDS; }
    if (rgba || bgra) {
        if (planes.planes[0].rowStride == 0) { planes.planes[0].rowStride = std::max(0, config.stride); }
        if (planes.planes[0].columnStride == 0) { planes.planes[0].columnStride = 4; }
        if (!PlaneFits(planes.planes[0], config.width, config.height, 4, capacity)) { return ERR_BOUNDS; }
    } else {
        const uint32_t chromaWidth = (config.width + 1) / 2, chromaHeight = (config.height + 1) / 2;
        if (planes.planeCount < 2 || !PlaneFits(planes.planes[0], config.width, config.height, 1, capacity)) {
            return ERR_BOUNDS;
        }
        if (planes.planeCount == 2) {
            if (!PlaneFits(planes.planes[1], chromaWidth, chromaHeight, 2, capacity)) { return ERR_BOUNDS; }
        } else if (!PlaneFits(planes.planes[1], chromaWidth, chromaHeight, 1, capacity) ||
            !PlaneFits(planes.planes[2], chromaWidth, chromaHeight, 1, capacity)) { return ERR_BOUNDS; }
    }
    OH_NativeBuffer_ColorSpace colorSpace = OH_COLORSPACE_NONE;
    OH_NativeBuffer_GetColorSpace(mapped.buffer, &colorSpace);
    bool bt709 = colorSpace == OH_COLORSPACE_BT709_FULL || colorSpace == OH_COLORSPACE_BT709_LIMIT;
    bool fullRange = colorSpace == OH_COLORSPACE_BT601_EBU_FULL ||
        colorSpace == OH_COLORSPACE_BT601_SMPTE_C_FULL || colorSpace == OH_COLORSPACE_BT709_FULL;
    if ((nv12 || nv21) && colorSpace != OH_COLORSPACE_NONE && !bt709 &&
        colorSpace != OH_COLORSPACE_BT601_EBU_FULL && colorSpace != OH_COLORSPACE_BT601_SMPTE_C_FULL &&
        colorSpace != OH_COLORSPACE_BT601_EBU_LIMIT && colorSpace != OH_COLORSPACE_BT601_SMPTE_C_LIMIT) {
        return ERR_FORMAT; // Do not mislabel HDR/P010/unknown non-SDR video as an SDR JPEG.
    }
    const double scale = std::min(1.0, std::min(static_cast<double>(options.maxWidth) / config.width,
        static_cast<double>(options.maxHeight) / config.height));
    out.width = std::max(1, static_cast<int32_t>(std::floor(config.width * scale)));
    out.height = std::max(1, static_cast<int32_t>(std::floor(config.height * scale)));
    out.timestampNs = timestamp;
    out.bytes.resize(static_cast<size_t>(out.width) * out.height * 4);
    const auto *base = static_cast<const uint8_t *>(mapped.address);
    for (int32_t y = 0; y < out.height; y++) {
        const uint32_t sy = static_cast<uint32_t>(static_cast<int64_t>(y) * config.height / out.height);
        for (int32_t x = 0; x < out.width; x++) {
            const uint32_t sx = static_cast<uint32_t>(static_cast<int64_t>(x) * config.width / out.width);
            uint8_t *pixel = out.bytes.data() + (static_cast<size_t>(y) * out.width + x) * 4;
            const auto &p0 = planes.planes[0];
            const uint64_t index = p0.offset + static_cast<uint64_t>(sy) * p0.rowStride +
                static_cast<uint64_t>(sx) * p0.columnStride;
            if (rgba || bgra) {
                pixel[0] = base[index + (rgba ? 2 : 0)]; pixel[1] = base[index + 1];
                pixel[2] = base[index + (rgba ? 0 : 2)]; pixel[3] = 255;
            } else {
                const auto &p1 = planes.planes[1];
                const uint64_t chroma = p1.offset + static_cast<uint64_t>(sy / 2) * p1.rowStride +
                    static_cast<uint64_t>(sx / 2) * p1.columnStride;
                uint8_t u = base[chroma + (planes.planeCount == 2 && nv21 ? 1 : 0)];
                uint8_t v;
                if (planes.planeCount == 2) { v = base[chroma + (nv21 ? 0 : 1)]; }
                else {
                    const auto &p2 = planes.planes[2];
                    v = base[p2.offset + static_cast<uint64_t>(sy / 2) * p2.rowStride +
                        static_cast<uint64_t>(sx / 2) * p2.columnStride];
                }
                const double yy = fullRange ? base[index] : (static_cast<double>(base[index]) - 16.0) * 255.0 / 219.0;
                const double uu = (static_cast<double>(u) - 128.0) * (fullRange ? 1.0 : 255.0 / 224.0);
                const double vv = (static_cast<double>(v) - 128.0) * (fullRange ? 1.0 : 255.0 / 224.0);
                pixel[2] = ClampColor(yy + (bt709 ? 1.5748 : 1.402) * vv);
                pixel[1] = ClampColor(yy - (bt709 ? 0.187324 : 0.344136) * uu - (bt709 ? 0.468124 : 0.714136) * vv);
                pixel[0] = ClampColor(yy + (bt709 ? 1.8556 : 1.772) * uu); pixel[3] = 255;
            }
        }
    }
    return 0;
}

struct PixelmapOwner {
    OH_Pixelmap_InitializationOptions *options = nullptr;
    OH_PixelmapNative *pixelmap = nullptr;
    ~PixelmapOwner()
    {
        if (pixelmap) { OH_PixelmapNative_Release(pixelmap); }
        if (options) { OH_PixelmapInitializationOptions_Release(options); }
    }
};
struct PackerOwner {
    OH_ImagePackerNative *packer = nullptr;
    OH_PackingOptions *options = nullptr;
    ~PackerOwner()
    {
        if (packer) { OH_ImagePackerNative_Release(packer); }
        if (options) { OH_PackingOptions_Release(options); }
    }
};

Frame ResizeBgra(const Frame &source, int32_t width, int32_t height)
{
    Frame result;
    result.width = width; result.height = height; result.timestampNs = source.timestampNs; result.revision = source.revision;
    result.bytes.resize(static_cast<size_t>(width) * height * 4);
    for (int32_t y = 0; y < height; y++) {
        const int32_t sy = static_cast<int64_t>(y) * source.height / height;
        for (int32_t x = 0; x < width; x++) {
            const int32_t sx = static_cast<int64_t>(x) * source.width / width;
            std::memcpy(result.bytes.data() + (static_cast<size_t>(y) * width + x) * 4,
                source.bytes.data() + (static_cast<size_t>(sy) * source.width + sx) * 4, 4);
        }
    }
    return result;
}

int32_t EncodeJpeg(const Frame &raw, size_t maxBytes, Frame &jpeg)
{
    constexpr uint32_t qualities[] = {80, 60, 40, 25};
    bool packerAccepted = false;
    // At most three sizes and four quality attempts. Never allocate an unbounded encoded buffer.
    for (int sizeAttempt = 0; sizeAttempt < 3; sizeAttempt++) {
        const double factor = sizeAttempt == 0 ? 1.0 : sizeAttempt == 1 ? 0.75 : 0.5;
        Frame image = ResizeBgra(raw, std::max(1, static_cast<int32_t>(raw.width * factor)),
            std::max(1, static_cast<int32_t>(raw.height * factor)));
        PixelmapOwner pm;
        if (OH_PixelmapInitializationOptions_Create(&pm.options) != IMAGE_SUCCESS ||
            OH_PixelmapInitializationOptions_SetWidth(pm.options, image.width) != IMAGE_SUCCESS ||
            OH_PixelmapInitializationOptions_SetHeight(pm.options, image.height) != IMAGE_SUCCESS ||
            OH_PixelmapInitializationOptions_SetSrcPixelFormat(pm.options, PIXEL_FORMAT_BGRA_8888) != IMAGE_SUCCESS ||
            OH_PixelmapInitializationOptions_SetPixelFormat(pm.options, PIXEL_FORMAT_BGRA_8888) != IMAGE_SUCCESS ||
            OH_PixelmapInitializationOptions_SetRowStride(pm.options, image.width * 4) != IMAGE_SUCCESS ||
            OH_PixelmapInitializationOptions_SetAlphaType(pm.options, PIXELMAP_ALPHA_TYPE_OPAQUE) != IMAGE_SUCCESS ||
            OH_PixelmapNative_CreatePixelmap(image.bytes.data(), image.bytes.size(), pm.options, &pm.pixelmap) != IMAGE_SUCCESS) {
            return ERR_ENCODE;
        }
        for (const uint32_t quality : qualities) {
            PackerOwner packer;
            Image_MimeType mime {const_cast<char *>("image/jpeg"), 10};
            if (OH_ImagePackerNative_Create(&packer.packer) != IMAGE_SUCCESS ||
                OH_PackingOptions_Create(&packer.options) != IMAGE_SUCCESS ||
                OH_PackingOptions_SetMimeType(packer.options, &mime) != IMAGE_SUCCESS ||
                OH_PackingOptions_SetQuality(packer.options, quality) != IMAGE_SUCCESS) { return ERR_ENCODE; }
            std::vector<uint8_t> output(maxBytes);
            size_t length = output.size();
            const auto code = OH_ImagePackerNative_PackToDataFromPixelmap(packer.packer, packer.options,
                pm.pixelmap, output.data(), &length);
            if (code == IMAGE_SUCCESS) {
                packerAccepted = true;
                if (length >= 4 && length <= maxBytes && output[0] == 0xff && output[1] == 0xd8 &&
                    output[length - 2] == 0xff && output[length - 1] == 0xd9) {
                    output.resize(length); jpeg.bytes = std::move(output);
                    jpeg.width = image.width; jpeg.height = image.height;
                    jpeg.timestampNs = raw.timestampNs; jpeg.revision = raw.revision; return 0;
                }
            }
        }
    }
    return packerAccepted ? ERR_JPEG_LIMIT : ERR_ENCODE;
}

class Session {
public:
    Options options;
    FrameBridge *frameBridge = nullptr;
    StateBridge *stateBridge = nullptr;
    napi_threadsafe_function frameFunction = nullptr;
    napi_threadsafe_function stateFunction = nullptr;
    std::atomic<bool> closing {false};
    std::atomic<bool> started {false};
    std::atomic<bool> paused {false};
    std::atomic<bool> everStarted {false};
    std::atomic<bool> platformEnded {false};

    ~Session() { StopAndJoin(false); }

    void EmitState(const char *state, int32_t error = 0)
    {
        if (!stateBridge || !stateFunction || !stateBridge->active.load()) { return; }
        bool notify = false;
        {
            std::lock_guard<std::mutex> lock(stateBridge->mutex);
            if (stateBridge->count == stateBridge->pending.size()) {
                for (size_t index = 1; index < stateBridge->count; index++) {
                    stateBridge->pending[index - 1] = stateBridge->pending[index];
                }
                stateBridge->count--;
            }
            stateBridge->pending[stateBridge->count++] = {state, error};
            if (!stateBridge->scheduled) { stateBridge->scheduled = true; notify = true; }
        }
        if (notify && napi_call_threadsafe_function(stateFunction, nullptr, napi_tsfn_nonblocking) != napi_ok) {
            std::lock_guard<std::mutex> lock(stateBridge->mutex); stateBridge->scheduled = false;
        }
    }

    void BeginStop()
    {
        closing.store(true); started.store(false);
        if (frameBridge) { frameBridge->active.store(false); }
        {
            std::lock_guard<std::mutex> lock(workMutex); pending.reset();
        }
        workReady.notify_all();
    }

    void Fail(int32_t code)
    {
        if (closing.load()) { return; }
        EmitState("error", code); BeginStop();
    }

    void Start()
    {
        worker = std::thread(&Session::Run, this);
        EmitState("start_requested");
        int32_t code = 0;
        try { code = ConfigureAndRequestConsent(); }
        catch (...) { code = ERR_MEMORY; }
        {
            std::lock_guard<std::mutex> lock(workMutex); setupFinished = true;
        }
        workReady.notify_all();
        if (code != 0) { Fail(code); }
    }

    int32_t ConfigureAndRequestConsent()
    {
        int32_t width = 0, height = 0;
        if (OH_NativeDisplayManager_GetDefaultDisplayWidth(&width) != DISPLAY_MANAGER_OK ||
            OH_NativeDisplayManager_GetDefaultDisplayHeight(&height) != DISPLAY_MANAGER_OK ||
            width <= 0 || height <= 0 || width > MAX_INPUT_DIMENSION || height > MAX_INPUT_DIMENSION) {
            return ERR_DISPLAY;
        }
        capture = OH_AVScreenCapture_Create();
        if (!capture) { return ERR_MEMORY; }
        OH_AVScreenCaptureConfig config {};
        config.captureMode = OH_CAPTURE_HOME_SCREEN;
        config.dataType = OH_ORIGINAL_STREAM;
        config.videoInfo.videoCapInfo.videoFrameWidth = width;
        config.videoInfo.videoCapInfo.videoFrameHeight = height;
        config.videoInfo.videoCapInfo.videoSource = OH_VIDEO_SOURCE_SURFACE_RGBA;
        // Both audio sources stay all-zero (ignored). Do not activate microphone/internal audio.
        auto code = OH_AVScreenCapture_SetStateCallback(capture, OnState, this);
        if (code == AV_SCREEN_CAPTURE_ERR_OK) { code = OH_AVScreenCapture_SetErrorCallback(capture, OnError, this); }
        if (code == AV_SCREEN_CAPTURE_ERR_OK) { code = OH_AVScreenCapture_SetDataCallback(capture, OnData, this); }
        if (code == AV_SCREEN_CAPTURE_ERR_OK) { code = OH_AVScreenCapture_SetMicrophoneEnabled(capture, false); }
        if (code == AV_SCREEN_CAPTURE_ERR_OK) { code = OH_AVScreenCapture_SetCanvasRotation(capture, true); }
        if (code == AV_SCREEN_CAPTURE_ERR_OK) { code = OH_AVScreenCapture_Init(capture, config); }
        if (code == AV_SCREEN_CAPTURE_ERR_OK) { code = OH_AVScreenCapture_StartScreenCapture(capture); }
        return static_cast<int32_t>(code);
    }

    int32_t StopAndJoin(bool abandon)
    {
        BeginStop();
        if (worker.joinable()) { worker.join(); }
        if (abandon && stateBridge) { stateBridge->active.store(false); }
        if (frameFunction) {
            napi_release_threadsafe_function(frameFunction, abandon ? napi_tsfn_abort : napi_tsfn_release);
            frameFunction = nullptr; frameBridge = nullptr;
        }
        if (stateFunction) {
            napi_release_threadsafe_function(stateFunction, abandon ? napi_tsfn_abort : napi_tsfn_release);
            stateFunction = nullptr; stateBridge = nullptr;
        }
        return cleanupError;
    }

    bool HasUnreleasedCapture() const { return capture != nullptr; }

private:
    OH_AVScreenCapture *capture = nullptr;
    std::thread worker;
    std::mutex workMutex;
    std::condition_variable workReady;
    std::unique_ptr<Frame> pending;
    std::mutex callbackMutex;
    int64_t lastTimestamp = 0;
    uint64_t revision = 0;
    bool setupFinished = false;
    int32_t cleanupError = 0;

    static void OnError(OH_AVScreenCapture *, int32_t errorCode, void *data)
    {
        static_cast<Session *>(data)->Fail(errorCode);
    }

    static void OnState(OH_AVScreenCapture *, OH_AVScreenCaptureStateCode state, void *data)
    {
        auto *self = static_cast<Session *>(data);
        if (self->closing.load()) { return; }
        switch (state) {
            case OH_SCREEN_CAPTURE_STATE_STARTED:
                self->everStarted.store(true);
                self->started.store(true); self->paused.store(false);
                self->EmitState("started"); self->workReady.notify_all(); break;
            case OH_SCREEN_CAPTURE_STATE_CANCELED:
                self->platformEnded.store(true);
                self->EmitState("canceled"); self->BeginStop(); break;
            case OH_SCREEN_CAPTURE_STATE_STOPPED_BY_USER:
                self->platformEnded.store(true);
                self->EmitState("stopped"); self->BeginStop(); break;
            case OH_SCREEN_CAPTURE_STATE_INTERRUPTED_BY_OTHER:
            case OH_SCREEN_CAPTURE_STATE_STOPPED_BY_CALL:
            case OH_SCREEN_CAPTURE_STATE_STOPPED_BY_USER_SWITCHES:
                self->platformEnded.store(true);
                self->EmitState("interrupted", static_cast<int32_t>(state)); self->BeginStop(); break;
            case OH_SCREEN_CAPTURE_STATE_ENTER_PRIVATE_SCENE: self->EmitState("private_scene"); break;
            case OH_SCREEN_CAPTURE_STATE_EXIT_PRIVATE_SCENE: self->EmitState("private_scene_exit"); break;
            case OH_SCREEN_CAPTURE_STATE_PAUSED_BY_USER:
            case OH_SCREEN_CAPTURE_STATE_PAUSED_BY_APP:
                self->paused.store(true); self->EmitState("paused", static_cast<int32_t>(state)); break;
            case OH_SCREEN_CAPTURE_STATE_RESUMED_BY_USER:
            case OH_SCREEN_CAPTURE_STATE_RESUMED_BY_APP:
                self->paused.store(false); self->EmitState("resumed"); break;
            default: break; // Microphone states cannot qualify a video-only stream as started.
        }
    }

    static void OnData(OH_AVScreenCapture *, OH_AVBuffer *buffer, OH_AVScreenCaptureBufferType type,
        int64_t timestamp, void *data)
    {
        auto *self = static_cast<Session *>(data);
        if (type != OH_SCREEN_CAPTURE_BUFFERTYPE_VIDEO || self->closing.load() ||
            !self->started.load() || self->paused.load()) { return; }
        std::unique_lock<std::mutex> callbackLock(self->callbackMutex, std::try_to_lock);
        if (!callbackLock.owns_lock()) { return; }
        if (timestamp <= self->lastTimestamp || (self->lastTimestamp > 0 &&
            timestamp - self->lastTimestamp < FRAME_INTERVAL_NS)) { return; }
        try {
            auto frame = std::make_unique<Frame>();
            const int32_t error = CopyFrame(buffer, timestamp, self->options, *frame);
            if (error != 0) { self->Fail(error); return; }
            self->lastTimestamp = timestamp; frame->revision = ++self->revision;
            {
                std::lock_guard<std::mutex> lock(self->workMutex);
                if (self->closing.load()) { return; }
                self->pending = std::move(frame); // Exactly one pending copied frame; latest wins.
            }
            self->workReady.notify_all();
        } catch (const std::bad_alloc &) { self->Fail(ERR_MEMORY); }
        catch (...) { self->Fail(ERR_BOUNDS); }
    }

    void Deliver(std::unique_ptr<Frame> frame)
    {
        if (closing.load() || !frameBridge || !frameFunction || !frameBridge->active.load()) { return; }
        bool notify = false;
        {
            std::lock_guard<std::mutex> lock(frameBridge->mutex);
            frameBridge->latest = std::move(frame);
            if (!frameBridge->scheduled) { frameBridge->scheduled = true; notify = true; }
        }
        if (notify && napi_call_threadsafe_function(frameFunction, nullptr, napi_tsfn_nonblocking) != napi_ok) {
            Fail(ERR_CALLBACK);
        }
    }

    void Run()
    {
        bool frameRateConfigured = false;
        while (!closing.load()) {
            std::unique_ptr<Frame> raw;
            {
                std::unique_lock<std::mutex> lock(workMutex);
                workReady.wait(lock, [this] { return closing.load() || (setupFinished &&
                    (pending || (started.load() && !frameRateConfiguredForWait))); });
                if (closing.load()) { break; }
                raw = std::move(pending);
            }
            if (!frameRateConfigured && started.load()) {
                // STARTED is real user consent; this does not generate artificial/idle frames.
                OH_AVScreenCapture_SetMaxVideoFrameRate(capture, 5);
                frameRateConfigured = true; frameRateConfiguredForWait = true;
            }
            if (!raw || paused.load()) { continue; }
            try {
                auto jpeg = std::make_unique<Frame>();
                const int32_t error = EncodeJpeg(*raw, options.maxBytes, *jpeg);
                if (error != 0) { Fail(error); break; }
                Deliver(std::move(jpeg));
            } catch (const std::bad_alloc &) { Fail(ERR_MEMORY); break; }
            catch (...) { Fail(ERR_ENCODE); break; }
        }
        // An asynchronous error/cancel may arrive during StartScreenCapture. Do not
        // release the capture object concurrently with its setup call stack.
        {
            std::unique_lock<std::mutex> lock(workMutex);
            workReady.wait(lock, [this] { return setupFinished; });
        }
        if (capture) {
            if (everStarted.load() && !platformEnded.load()) {
                const auto stopCode = OH_AVScreenCapture_StopScreenCapture(capture);
                if (stopCode != AV_SCREEN_CAPTURE_ERR_OK) { cleanupError = static_cast<int32_t>(stopCode); }
            }
            // Stop first, then wait for any in-progress copy; do not hold the callback mutex during Stop.
            std::lock_guard<std::mutex> lock(callbackMutex);
            const auto releaseCode = OH_AVScreenCapture_Release(capture);
            if (releaseCode == AV_SCREEN_CAPTURE_ERR_OK) { capture = nullptr; }
            else { cleanupError = static_cast<int32_t>(releaseCode); }
        }
        EmitState(cleanupError == 0 ? "stopped" : "error", cleanupError);
    }
    bool frameRateConfiguredForWait = false;
};

std::mutex activeMutex;
std::unique_ptr<Session> active;

int32_t StopActive(bool abandon)
{
    std::unique_ptr<Session> previous;
    { std::lock_guard<std::mutex> lock(activeMutex); previous = std::move(active); }
    if (!previous) { return 0; }
    const int32_t code = previous->StopAndJoin(abandon);
    if (previous->HasUnreleasedCapture()) {
        // A failed platform Release does not establish callback/lifetime quiescence.
        // Quarantine this userdata and prevent a replacement session, rather than
        // free it while a platform callback might still hold its address.
        if (abandon) { previous.release(); } // Process teardown owns final OS cleanup.
        else { std::lock_guard<std::mutex> lock(activeMutex); active = std::move(previous); }
    }
    return code;
}

napi_value Undefined(napi_env env)
{
    napi_value value = nullptr; napi_get_undefined(env, &value); return value;
}

bool ReadInteger(napi_env env, napi_value object, const char *name, int32_t minimum,
    int32_t maximum, int32_t &value)
{
    napi_value property = nullptr;
    double number = 0;
    if (napi_get_named_property(env, object, name, &property) != napi_ok ||
        napi_get_value_double(env, property, &number) != napi_ok || !std::isfinite(number) ||
        number != std::floor(number) || number < minimum || number > maximum) { return false; }
    value = static_cast<int32_t>(number); return true;
}

napi_value Start(napi_env env, napi_callback_info info)
{
    napi_value args[3] = {nullptr, nullptr, nullptr}; size_t argc = 3;
    napi_get_cb_info(env, info, &argc, args, nullptr, nullptr);
    Options options; int32_t maxBytes = 0;
    napi_valuetype stateType = napi_undefined, frameType = napi_undefined;
    if (argc == 3) { napi_typeof(env, args[1], &stateType); napi_typeof(env, args[2], &frameType); }
    if (argc != 3 || stateType != napi_function || frameType != napi_function ||
        !ReadInteger(env, args[0], "maxWidth", 32, 640, options.maxWidth) ||
        !ReadInteger(env, args[0], "maxHeight", 32, 640, options.maxHeight) ||
        !ReadInteger(env, args[0], "maxBytes", 1024, 42000, maxBytes)) {
        napi_throw_error(env, "-1001", "screen_share_options_invalid"); return Undefined(env);
    }
    options.maxBytes = static_cast<size_t>(maxBytes);
    {
        std::lock_guard<std::mutex> lock(activeMutex);
        if (active && !active->closing.load()) {
            napi_throw_error(env, "-1010", "screen_share_already_started"); return Undefined(env);
        }
    }
    const int32_t previousStop = StopActive(false);
    if (previousStop != 0) {
        napi_throw_error(env, std::to_string(previousStop).c_str(), "screen_share_native_stop_failed");
        return Undefined(env);
    }
    try {
        auto session = std::make_unique<Session>(); session->options = options;
        napi_value resourceName = nullptr;
        napi_create_string_utf8(env, "amitaScreenShareState", NAPI_AUTO_LENGTH, &resourceName);
        auto *states = new StateBridge();
        if (napi_create_threadsafe_function(env, args[1], nullptr, resourceName, 1, 1,
            states, DeleteStateBridge, states, CallStateJs, &session->stateFunction) != napi_ok) {
            delete states; napi_throw_error(env, "-1008", "screen_share_state_callback_failed"); return Undefined(env);
        }
        session->stateBridge = states;
        napi_create_string_utf8(env, "amitaScreenShareFrame", NAPI_AUTO_LENGTH, &resourceName);
        auto *frames = new FrameBridge();
        if (napi_create_threadsafe_function(env, args[2], nullptr, resourceName, 1, 1,
            frames, DeleteFrameBridge, frames, CallFrameJs, &session->frameFunction) != napi_ok) {
            delete frames; napi_throw_error(env, "-1008", "screen_share_frame_callback_failed"); return Undefined(env);
        }
        session->frameBridge = frames;
        Session *pointer = session.get();
        { std::lock_guard<std::mutex> lock(activeMutex); active = std::move(session); }
        pointer->Start();
    } catch (...) {
        StopActive(false); napi_throw_error(env, "-1009", "screen_share_native_allocation_failed");
    }
    return Undefined(env);
}

napi_value Stop(napi_env env, napi_callback_info)
{
    const int32_t code = StopActive(false);
    if (code != 0) { napi_throw_error(env, std::to_string(code).c_str(), "screen_share_native_stop_failed"); }
    return Undefined(env);
}

void Cleanup(void *) { StopActive(true); }

napi_value Init(napi_env env, napi_value exports)
{
    napi_property_descriptor methods[] = {
        {"start", nullptr, Start, nullptr, nullptr, nullptr, napi_default, nullptr},
        {"stop", nullptr, Stop, nullptr, nullptr, nullptr, napi_default, nullptr}
    };
    napi_define_properties(env, exports, 2, methods);
    napi_add_env_cleanup_hook(env, Cleanup, nullptr);
    return exports;
}
} // namespace

static napi_module module = {1, 0, nullptr, Init, "amita_screenshare", nullptr, {0}};
extern "C" __attribute__((constructor)) void RegisterAmitaScreenShare() { napi_module_register(&module); }

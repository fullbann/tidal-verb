// wav.h - load a WAV file into memory (PCM 8/16/24/32-bit or float32, any channel count).
//
// SPDX-License-Identifier: MIT
//
//   dsp::WavData w;
//   if (dsp::loadWav(mpc::contentPath("Samples/kick.wav"), w)) { /* w.channels[0][i] ... */ }
//
// NOT realtime-safe (allocates, reads a file): call from the constructor / prepare() /
// loadExtraState(), keep the result in a member, and only read it in process().
// Samples are converted to float in -1..1. `sampleRate` is the file's rate; resample or
// scale your playback increment by w.sampleRate / hostSampleRate.

#ifndef MPC_DSP_WAV_H
#define MPC_DSP_WAV_H

#include "../mpc_resources.h"

#include <cstdint>
#include <cstring>
#include <string>
#include <vector>

namespace dsp {

struct WavData
{
    double sampleRate = 44100.0;
    std::vector<std::vector<float>> channels;   // channels[c][frame]
    size_t numFrames() const { return channels.empty() ? 0 : channels[0].size(); }
    int numChannels() const { return (int)channels.size(); }
};

inline bool parseWav(const std::vector<uint8_t>& d, WavData& out)
{
    auto u16 = [&](size_t p) { return (uint32_t)d[p] | ((uint32_t)d[p + 1] << 8); };
    auto u32 = [&](size_t p) { return u16(p) | (u16(p + 2) << 16); };
    out = WavData();
    if (d.size() < 12 || std::memcmp(d.data(), "RIFF", 4) != 0 || std::memcmp(d.data() + 8, "WAVE", 4) != 0) return false;

    uint32_t format = 0, channels = 0, rate = 0, bits = 0;
    size_t dataPos = 0, dataLen = 0;
    for (size_t p = 12; p + 8 <= d.size();)
    {
        const uint32_t len = u32(p + 4);
        const size_t body = p + 8;
        const size_t avail = d.size() - body;
        if (std::memcmp(d.data() + p, "fmt ", 4) == 0 && len >= 16 && avail >= 16)
        {
            format = u16(body); channels = u16(body + 2); rate = u32(body + 4); bits = u16(body + 14);
            if (format == 0xFFFE && len >= 26 && avail >= 26) format = u16(body + 24);   // WAVE_FORMAT_EXTENSIBLE
        }
        else if (std::memcmp(d.data() + p, "data", 4) == 0)
        {
            dataPos = body;
            dataLen = len < avail ? len : avail;   // tolerate truncated / streamed files
            break;
        }
        p = body + len + (len & 1u);
    }
    if (dataPos == 0 || channels < 1 || channels > 16 || rate == 0) return false;
    const bool isFloat = (format == 3 && bits == 32);
    if (!isFloat && !(format == 1 && (bits == 8 || bits == 16 || bits == 24 || bits == 32))) return false;

    const size_t bytes = bits / 8;
    const size_t frames = dataLen / (bytes * channels);
    out.sampleRate = (double)rate;
    out.channels.assign(channels, std::vector<float>(frames));
    const uint8_t* s = d.data() + dataPos;
    for (size_t i = 0; i < frames; ++i)
        for (uint32_t c = 0; c < channels; ++c, s += bytes)
        {
            float v;
            if (isFloat)       { uint32_t u = (uint32_t)s[0] | ((uint32_t)s[1] << 8) | ((uint32_t)s[2] << 16) | ((uint32_t)s[3] << 24); std::memcpy(&v, &u, 4); }
            else if (bits == 8)  v = ((int)s[0] - 128) / 128.0f;
            else if (bits == 16) v = (int16_t)(s[0] | (s[1] << 8)) / 32768.0f;
            else if (bits == 24) v = ((int32_t)(((uint32_t)s[0] << 8) | ((uint32_t)s[1] << 16) | ((uint32_t)s[2] << 24)) >> 8) / 8388608.0f;
            else                 v = (int32_t)((uint32_t)s[0] | ((uint32_t)s[1] << 8) | ((uint32_t)s[2] << 16) | ((uint32_t)s[3] << 24)) / 2147483648.0f;
            out.channels[c][i] = v;
        }
    return frames > 0;
}

inline bool loadWav(const std::string& path, WavData& out)
{
    std::vector<uint8_t> bytes;
    return mpc::readFile(path, bytes) && parseWav(bytes, out);
}

} // namespace dsp

#endif // MPC_DSP_WAV_H

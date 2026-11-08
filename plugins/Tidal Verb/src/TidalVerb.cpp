// Tidal Verb - a Polara-style stereo reverb for the Akai MPC / Force, by Fullban.
//
// SPDX-License-Identifier: MIT
//
// Seven reverb types, inspired by the feel of the Digitech Polara pedal:
//   Room, Hall, Spring, Plate, Modulated, Reverse, Halo (octave-up shimmer).
// This is an original design (it is NOT a copy of Lexicon/Digitech algorithms).
//
// Signal flow:
//   in -> [type pre-stage: spring dispersion | reverse grains] -> input diffusers
//      -> 8-line feedback delay network (FDN) with damping + Householder mixing
//      -> (Halo: octave-up pitch shifter fed back into the network)
//      -> wet. out = dry*cos(level) + wet*sin(level)   (equal-power wet/dry)
//
// Parameters (order is permanent once users have projects):
//   0 Level        wet/dry mix (harmless to nudge: it is restored by the host)
//   1 Type         Room / Hall / Spring / Plate / Modulated / Reverse / Halo
//   2 Decay        tail length, about 0.3 s .. 6 s (scaled a bit per type)
//   3 Liveliness   high-frequency damping: low = dark/tamed, high = bright
//   4 Tails        On = tail rings out. Off = wet is muted shortly after the input goes silent
//
// CPU: 8 delay lines with per-sample modulation, a few allpasses. Expect a few percent of one core.

#include "mpc_plugin.h"
#include "dsp/math.h"
#include "dsp/delay_line.h"
#include "dsp/onepole.h"
#include "dsp/smoother.h"

#include <vector>
#include <algorithm>
#include <cmath>

using namespace mpc::dsp;

namespace {

enum ParamIndex { kLevel = 0, kType, kDecay, kLiveliness, kTails, kSwirl, kSwirlRate, kWarmth, kMeter, kAir, kNumParams };
enum ReverbType { kRoom = 0, kHall, kSpring, kPlate, kModulated, kReverse, kHalo, kHaloPlus, kAbyss, kCascade, kNumTypes };

constexpr int   kLines = 8;
constexpr int   kSub   = 16;   // control-rate block (samples)

// Per-type character: delay size scale, decay multiplier, modulation depth (samples), pre-diffusion gain
// size/decay/modDepth/diffuse as before, plus: rate (modulation speed scale), damp (high-cut scale),
// swirl (slow vibrato depth in ms on the wet output, 0 = off), shim (shimmer feedback amount, 0 = off),
// shim2 / shim3 (levels of a second and third octave stacked on the first), shimBright (shimmer low-pass coefficient)
struct TypeInfo { float size, decay, modDepth, diffuse, rate, damp, swirl, shim, shim2, shimBright, shim3; };
const TypeInfo kTypes[kNumTypes] = {
    /* Room      */ { 0.50f, 0.55f,  0.4f, 0.65f, 1.0f, 1.0f, 0.0f, 0.00f, 0.0f, 0.5f, 0.0f },
    /* Hall      */ { 1.55f, 1.40f,  1.0f, 0.60f, 1.0f, 1.0f, 0.0f, 0.00f, 0.0f, 0.5f, 0.0f },
    /* Spring    */ { 0.42f, 0.85f,  0.0f, 0.30f, 1.0f, 1.0f, 0.0f, 0.00f, 0.0f, 0.5f, 0.0f },
    /* Plate     */ { 0.85f, 1.00f,  2.2f, 0.72f, 1.0f, 1.0f, 0.0f, 0.00f, 0.0f, 0.5f, 0.0f },
    /* Modulated */ { 1.30f, 1.20f, 14.0f, 0.60f, 1.0f, 1.0f, 0.0f, 0.00f, 0.0f, 0.5f, 0.0f },
    /* Reverse   */ { 0.60f, 0.60f,  0.4f, 0.65f, 1.0f, 1.0f, 0.0f, 0.00f, 0.0f, 0.5f, 0.0f },
    /* Halo      */ { 1.45f, 1.50f,  2.5f, 0.60f, 1.0f, 1.0f, 0.0f, 0.12f, 0.0f, 0.5f, 0.0f },
    /* Halo+     */ { 1.85f, 1.60f,  3.0f, 0.60f, 1.0f, 1.40f, 0.0f, 0.13f, 0.55f, 0.80f, 0.0f },
    /* Abyss     */ { 1.90f, 1.70f, 18.0f, 0.62f, 0.35f, 0.30f, 9.0f, 0.00f, 0.0f, 0.5f, 0.0f },
    /* Cascade   */ { 1.95f, 1.70f,  3.5f, 0.60f, 1.00f, 1.40f, 3.0f, 0.125f, 0.75f, 0.92f, 0.45f },
};

// Base delay lengths in samples at 44.1 kHz (mutually prime-ish).
const float kBase[kLines] = { 1087, 1283, 1481, 1699, 1907, 2113, 2339, 2551 };
const float kModRate[kLines] = { 0.31f, 0.43f, 0.37f, 0.53f, 0.29f, 0.47f, 0.41f, 0.59f };

// High-cut frequency for a type at a Liveliness setting (0..1). Abyss has a wider range so it can go very bright.
inline float cutoffFor(int type, float live, double sr)
{
    float fc = (type == kAbyss) ? 450.0f * std::pow(10.0f, live * 1.7f)
                                : 1500.0f * std::pow(10.0f, live) * kTypes[type].damp;
    return std::min(fc, 0.45f * (float)sr);
}

// Schroeder allpass with its own buffer (allocated in prepare()).
struct Allpass
{
    std::vector<float> buf;
    int idx = 0;
    float g = 0.6f;
    void prepare(int len) { buf.assign((size_t)std::max(len, 2), 0.0f); idx = 0; }
    void reset() { std::fill(buf.begin(), buf.end(), 0.0f); idx = 0; }
    float process(float x)
    {
        const float d = buf[(size_t)idx];
        const float v = x - g * d;
        buf[(size_t)idx] = v;
        if (++idx >= (int)buf.size()) idx = 0;
        return d + g * v;
    }
};

// Two-tap crossfaded delay-line pitch shifter, +1 octave.
struct OctaveUp
{
    std::vector<float> buf;
    int size = 4096, w = 0;
    float ph = 0.0f;
    float lp = 0.0f;
    float lpA = 0.5f;   // output low-pass coefficient (higher = brighter)
    static constexpr float kWindow = 2048.0f;
    void prepare() { buf.assign((size_t)size, 0.0f); w = 0; ph = 0.0f; lp = 0.0f; }
    void reset() { std::fill(buf.begin(), buf.end(), 0.0f); w = 0; ph = 0.0f; lp = 0.0f; }
    float process(float x)
    {
        buf[(size_t)w] = x;
        ph += 1.0f / kWindow; if (ph >= 1.0f) ph -= 1.0f;
        float out = 0.0f;
        for (int t = 0; t < 2; ++t)
        {
            float p = ph + 0.5f * (float)t; if (p >= 1.0f) p -= 1.0f;
            float pos = (float)w - ((1.0f - p) * kWindow + 1.0f);
            if (pos < 0.0f) pos += (float)size;
            const int i0 = (int)pos; const float fr = pos - (float)i0;
            const int i1 = (i0 + 1 >= size) ? 0 : i0 + 1;
            const float gain = 0.5f - 0.5f * std::cos(p * kTwoPi);
            out += gain * (buf[(size_t)i0] * (1.0f - fr) + buf[(size_t)i1] * fr);
        }
        if (++w >= size) w = 0;
        lp += lpA * (out - lp);
        return lp;
    }
};

// Reverse grains: two read heads sweep backwards through a recent-input buffer, Hann-crossfaded.
struct ReverseGrains
{
    std::vector<float> buf;
    int size = 0, w = 0;
    float window = 1.0f, ph = 0.0f;
    void prepare(double sr)
    {
        window = (float)(0.55 * sr);
        size = (int)window + 8;
        buf.assign((size_t)size, 0.0f); w = 0; ph = 0.0f;
    }
    void reset() { std::fill(buf.begin(), buf.end(), 0.0f); w = 0; ph = 0.0f; }
    float process(float x)
    {
        buf[(size_t)w] = x;
        ph += 2.0f / window; if (ph >= 1.0f) ph -= 1.0f;   // delay grows 2 samples per sample = reversed playback
        float out = 0.0f;
        for (int t = 0; t < 2; ++t)
        {
            float p = ph + 0.5f * (float)t; if (p >= 1.0f) p -= 1.0f;
            float pos = (float)w - (p * (window - 2.0f) + 1.0f);
            if (pos < 0.0f) pos += (float)size;
            const int i0 = (int)pos; const float fr = pos - (float)i0;
            const int i1 = (i0 + 1 >= size) ? 0 : i0 + 1;
            const float gain = 0.5f - 0.5f * std::cos(p * kTwoPi);
            out += gain * (buf[(size_t)i0] * (1.0f - fr) + buf[(size_t)i1] * fr);
        }
        if (++w >= size) w = 0;
        return out;
    }
};

} // namespace

class TidalVerb : public mpc::Plugin
{
public:
    static mpc::PluginInfo makeInfo()
    {
        static const char kUid[] = "FEwF";   // must match plugin.json "uid"
        mpc::PluginInfo info;
        info.name         = "Tidal Verb";
        info.vendor       = "Fullban";
        info.uniqueId     = MPC_FOURCC(kUid[0], kUid[1], kUid[2], kUid[3]);
        info.version      = 1000;
        info.isInstrument = false;
        info.numInputs    = 2;
        info.numOutputs   = 2;
        info.tailSamples  = 0;
        return info;
    }

    TidalVerb() : mpc::Plugin(makeInfo())
    {
        using namespace mpc;
        addParam(floatParam ("level",  "Level",       0.0f, 100.0f, 40.0f, "%", 0));
        addParam(choiceParam("type",   "Type", { "Room", "Hall", "Spring", "Plate", "Modulated", "Reverse", "Halo", "Halo+", "Abyss", "Cascade" }, kAbyss));
        addParam(floatParam ("decay",  "Decay",       0.0f, 100.0f, 100.0f, "%", 0));
        addParam(floatParam ("live",   "Liveliness",  0.0f, 100.0f, 60.0f, "%", 0));
        addParam(boolParam  ("tails",  "Tails", true));
        addParam(floatParam ("swirl",  "Swirl",       0.0f, 100.0f, 60.0f, "%", 0));   // depth of the slow pitch drift (Abyss, Cascade)
        addParam(floatParam ("swirlrate", "Swirl Rate", 0.0f, 100.0f, 40.0f, "%", 0)); // speed of the drift, about 0.07 Hz .. 0.7 Hz
        addParam(floatParam ("warmth", "Warmth",      0.0f, 100.0f, 35.0f, "%", 0));   // low-end boost in the tail
        {   // read-only output level for the skin's live trace; the plugin writes it, the user does not
            Param m = floatParam("wetlevel", "Wet Level", 0.0f, 100.0f, 0.0f, "%", 0);
            m.automatable = false;
            addParam(m);
        }
        addParam(floatParam ("air",    "Air",         0.0f, 100.0f, 45.0f, "%", 0));   // high-shelf sparkle on the wet signal (appended in v1.3)
        if (numParams() != kNumParams) mpc::log("TidalVerb: parameter count mismatch");

        //                                      level type            decay live tails swirl rate warm meter air
        addProgram("Abyss",        { 40, (float)kAbyss,     100, 60, 1, 60, 40, 35, 0, 45 });
        addProgram("Abyss Bright", { 40, (float)kAbyss,     100, 90, 1, 60, 40, 35, 0, 65 });
        addProgram("Cascade",      { 45, (float)kCascade,   100, 100, 1, 50, 40, 20, 0, 55 });
        addProgram("Halo Plus",    { 45, (float)kHaloPlus,  100, 100, 1, 0, 40, 0, 0, 50 });
        addProgram("Halo",         { 40, (float)kHalo,      65, 70, 1, 0, 40, 0, 0, 40 });
        addProgram("Hall",         { 35, (float)kHall,      55, 60, 1, 0, 40, 0, 0, 30 });
        addProgram("Small Room",   { 30, (float)kRoom,      40, 55, 1, 0, 40, 0, 0, 25 });
        addProgram("Spring Tank",  { 40, (float)kSpring,    50, 65, 1, 0, 40, 0, 0, 30 });
        addProgram("Bright Plate", { 35, (float)kPlate,     50, 85, 1, 0, 40, 0, 0, 45 });
        addProgram("Lush Mod",     { 40, (float)kModulated, 65, 55, 1, 0, 40, 0, 0, 35 });
        addProgram("Swell",        { 45, (float)kReverse,   50, 60, 1, 0, 40, 0, 0, 30 });
    }

    void prepare(double sampleRate, int /*maxBlockSize*/) override
    {
        srScale_ = (float)(sampleRate / 44100.0);
        const int maxLen = (int)(2551.0f * 2.0f * srScale_) + 96;
        for (int i = 0; i < kLines; ++i) line_[i].prepare(maxLen);
        const int apLen[4] = { 142, 107, 379, 277 };
        for (int c = 0; c < 2; ++c)
            for (int k = 0; k < 4; ++k) diff_[c][k].prepare((int)((float)apLen[k] * srScale_) + 2);
        const int dispLen[kDisp] = { 17, 19, 23, 29, 31, 37, 41, 43, 47, 53, 59, 61 };
        for (int k = 0; k < kDisp; ++k) { disp_[k].prepare((int)((float)dispLen[k] * srScale_) + 2); disp_[k].g = 0.68f; }
        spring_.prepare((int)(0.06 * sampleRate) + 8);
        for (int c = 0; c < 2; ++c) { shim_[c].prepare(); shim2_[c].prepare(); swirl_[c].prepare((int)(0.05 * sampleRate) + 8); }
        for (int i = 0; i < 2; ++i) swirlPhase_[i] = 0.25f * (float)i;
        for (int c = 0; c < 2; ++c) { warmLp_[c].setCutoff(220.0f, sampleRate); shim3_[c].prepare(); }
        rev_[0].prepare(sampleRate); rev_[1].prepare(sampleRate);
        const double subRate = sampleRate / (double)kSub;
        swirlDepth_.setTime(120.0f, subRate); swirlRate_.setTime(200.0f, subRate); warm_.setTime(60.0f, subRate); air_.setTime(60.0f, subRate);
        for (int c = 0; c < 2; ++c) airLp_[c].setCutoff(3000.0f, sampleRate);
        size_.setTime(150.0f, subRate);
        t60_.setTime(60.0f, subRate);
        damp_.setTime(60.0f, subRate);
        level_.setTime(30.0f, subRate);
        gate_.setTime(40.0f, subRate);
        for (int i = 0; i < kLines; ++i) phase_[i] = (float)i / (float)kLines;
    }

    // The Wet Level parameter is a live readout for the skin, not a setting: it shows fixed text
    // (its value is rewritten by the audio thread on every block).
    bool formatValue(int index, float /*plainValue*/, std::string& out) override
    {
        if (index != kMeter) return false;
        out = "live";
        return true;
    }

    void reset() override
    {
        for (int i = 0; i < kLines; ++i) { line_[i].reset(); lp_[i].reset(); }
        for (int c = 0; c < 2; ++c) { for (int k = 0; k < 4; ++k) diff_[c][k].reset(); shim_[c].reset(); shim2_[c].reset(); shim3_[c].reset(); swirl_[c].reset(); warmLp_[c].reset(); airLp_[c].reset(); rev_[c].reset(); }
        for (int k = 0; k < kDisp; ++k) disp_[k].reset();
        spring_.reset(); springLp_.reset(); springFb_ = 0.0f;
        shimOut_[0] = shimOut_[1] = 0.0f; meterPeak_ = 0.0f; meterEnv_ = 0.0f;
        env_ = 0.0f; hold_ = 0; gateClosedCount_ = 0;
        snapControls();
    }

    void process(mpc::ProcessContext& ctx) override
    {
        const int type = mpc::clamp(getInt(kType), 0, (int)kNumTypes - 1);
        const TypeInfo& ti = kTypes[type];
        const bool tailsOn = getBool(kTails);
        const double sr = ctx.sampleRate;

        const float* inLp = ctx.inputs[0];
        const float* inRp = ctx.numInputs > 1 ? ctx.inputs[1] : ctx.inputs[0];
        float* outL = ctx.outputs[0];
        float* outR = ctx.numOutputs > 1 ? ctx.outputs[1] : ctx.outputs[0];

        const float holdSamples = (float)(0.25 * sr);
        const float envRelease = 1.0f - std::exp(-1.0f / (float)(0.05 * sr));

        for (int start = 0; start < ctx.numFrames; start += kSub)
        {
            const int cnt = std::min(kSub, ctx.numFrames - start);

            // ---- control rate -------------------------------------------------
            size_.setTarget(ti.size);
            const float decayNorm = get(kDecay) * 0.01f;
            float t60 = 0.3f * std::pow(20.0f, decayNorm) * ti.decay;   // 0.3 .. 6 s, scaled by type
            t60 = mpc::clamp(t60, 0.1f, 14.0f);
            t60_.setTarget(t60);
            const float live = get(kLiveliness) * 0.01f;
            const float fc = cutoffFor(type, live, sr);
            damp_.setTarget(fc);
            level_.setTarget(get(kLevel) * 0.01f);

            const float size = size_.process();
            const float t60s = t60_.process();
            const float fcs = damp_.process();
            const float lvl = level_.process();
            const float dryG = std::cos(lvl * kHalfPi);
            const float wetG = std::sin(lvl * kHalfPi);

            float len[kLines], g[kLines];
            const float lpA = 1.0f - std::exp(-kTwoPi * fcs / (float)sr);
            for (int i = 0; i < kLines; ++i)
            {
                len[i] = kBase[i] * size * srScale_;
                g[i] = std::pow(10.0f, -3.0f * len[i] / (t60s * (float)sr));
                lp_[i].setCoeff(lpA);
            }
            const bool halo = ti.shim > 0.0f;
            const bool swirl = ti.swirl > 0.0f;
            // keep the wet level similar across types and decay settings (steady-state level ~ sqrt(T60 / delay length))
            const float avgLenSec = (1807.5f * size * srScale_) / (float)sr;
            const float inGain = mpc::clamp(kInK * std::sqrt(avgLenSec / t60s), 0.02f, 0.6f);
            const float lineScale = halo ? (ti.shim3 > 0.0f ? 0.84f : (ti.shim2 > 0.0f ? 0.90f : 0.94f)) : 1.0f;   // Halo feeds energy back, so the plain tank decays a little faster
            const float shimK = ti.shim;
            swirlDepth_.setTarget(16.0f * get(kSwirl) * 0.01f);              // ms
            swirlRate_.setTarget(0.07f * std::pow(10.0f, get(kSwirlRate) * 0.01f));   // Hz
            warm_.setTarget(get(kWarmth) * 0.01f);
            air_.setTarget(get(kAir) * 0.01f);
            const float swirlSamples = swirlDepth_.process() * 0.001f * (float)sr;
            const float swirlInc = swirlRate_.process() / (float)sr;
            const float warm = warm_.process();
            const float airG = air_.process() * 3.5f;   // up to about +13 dB above ~6 kHz
            for (int c = 0; c < 2; ++c) { shim_[c].lpA = ti.shimBright; shim2_[c].lpA = ti.shimBright; shim3_[c].lpA = ti.shimBright; }
            const float modDepth = ti.modDepth * srScale_;
            const float diffG = ti.diffuse;
            for (int c = 0; c < 2; ++c) for (int k = 0; k < 4; ++k) diff_[c][k].g = diffG;
            const bool spring = (type == kSpring);
            const bool reverse = (type == kReverse);

            // ---- audio rate ---------------------------------------------------
            for (int n = 0; n < cnt; ++n)
            {
                const int i = start + n;
                const float dryL = inLp[i], dryR = inRp[i];

                // tails gate (only matters when Tails is off)
                const float a = std::max(std::fabs(dryL), std::fabs(dryR));
                env_ = a > env_ ? a : env_ + envRelease * (a - env_);
                if (env_ > 0.0003f) hold_ = (int)holdSamples; else if (hold_ > 0) --hold_;

                float xL = dryL, xR = dryR;
                if (spring)
                {
                    float m = 0.5f * (dryL + dryR);
                    float s = m + springFb_ * 0.45f;
                    for (int k = 0; k < kDisp; ++k) s = disp_[k].process(s);
                    spring_.write(s);
                    springFb_ = springLp_.process(spring_.readLinear(0.034f * (float)sr));
                    xL = xR = s * 0.8f;
                }
                else if (reverse)
                {
                    xL = rev_[0].process(dryL);
                    xR = rev_[1].process(dryR);
                }

                // input diffusion
                for (int k = 0; k < 4; ++k) { xL = diff_[0][k].process(xL); xR = diff_[1][k].process(xR); }

                // Halo: octave-up shifted wet is fed back into the network
                float injL = 0.0f, injR = 0.0f;
                if (halo) { injL = shimOut_[0]; injR = shimOut_[1]; }

                // read + damp + decay
                float v[kLines], sum = 0.0f;
                for (int l = 0; l < kLines; ++l)
                {
                    phase_[l] += kModRate[l] * ti.rate / (float)sr; if (phase_[l] >= 1.0f) phase_[l] -= 1.0f;
                    const float d = len[l] + modDepth * fastSin01(phase_[l]);
                    float s = line_[l].readLinear(d);
                    s = lp_[l].process(s) * g[l];
                    v[l] = flushDenormal(s * lineScale);
                    sum += v[l];
                }
                const float k2 = sum * (2.0f / (float)kLines);
                for (int l = 0; l < kLines; ++l)
                {
                    const float in = ((l & 1) ? xR : xL) * inGain + ((l & 1) ? injR : injL) * 0.35f;
                    line_[l].write(v[l] - k2 + in);
                }

                float wetL = 0.0f, wetR = 0.0f;
                for (int l = 0; l < kLines; l += 2) { wetL += v[l]; wetR += v[l + 1]; }
                wetL *= 0.5f; wetR *= 0.5f;

                if (halo)
                {
                    float s1L = shim_[0].process(wetL), s1R = shim_[1].process(wetR);
                    if (ti.shim2 > 0.0f)
                    {
                        const float s2L = shim2_[0].process(s1L), s2R = shim2_[1].process(s1R);   // second octave, shifted from the first
                        float s3L = 0.0f, s3R = 0.0f;
                        if (ti.shim3 > 0.0f) { s3L = shim3_[0].process(s2L); s3R = shim3_[1].process(s2R); }   // third octave
                        s1L += ti.shim2 * s2L + ti.shim3 * s3L;
                        s1R += ti.shim2 * s2R + ti.shim3 * s3R;
                    }
                    shimOut_[0] = fastTanh(s1L * shimK * 2.0f) * 0.5f;
                    shimOut_[1] = fastTanh(s1R * shimK * 2.0f) * 0.5f;
                    wetL += shimOut_[0]; wetR += shimOut_[1];
                }

                if (swirl)
                {
                    // slow stereo vibrato (left and right a quarter turn apart) = drifting underwater pitch wobble
                    swirlPhase_[0] += swirlInc; if (swirlPhase_[0] >= 1.0f) swirlPhase_[0] -= 1.0f;
                    swirlPhase_[1] += swirlInc; if (swirlPhase_[1] >= 1.0f) swirlPhase_[1] -= 1.0f;
                    swirl_[0].write(wetL); swirl_[1].write(wetR);
                    const float baseD = swirlSamples * 1.2f + 2.0f;
                    const float vL = swirl_[0].readLinear(baseD + swirlSamples * fastSin01(swirlPhase_[0]));
                    const float vR = swirl_[1].readLinear(baseD + swirlSamples * fastSin01(swirlPhase_[1] + 0.25f));
                    wetL = 0.3f * wetL + 0.7f * vL;
                    wetR = 0.3f * wetR + 0.7f * vR;
                }

                if (warm > 0.0f)
                {
                    wetL += warm * warmLp_[0].process(wetL);
                    wetR += warm * warmLp_[1].process(wetR);
                }

                if (airG > 0.0f)
                {
                    wetL += airG * (wetL - airLp_[0].process(wetL));
                    wetR += airG * (wetR - airLp_[1].process(wetR));
                }

                // Tails Off: close the wet path once the input has been silent for a while
                float gateTarget = (tailsOn || hold_ > 0) ? 1.0f : 0.0f;
                gateSm_ += gateCoeff_ * (gateTarget - gateSm_);
                if (gateSm_ < 0.0005f) { if (++gateClosedCount_ == (int)(0.5 * sr)) clearTank(); }
                else gateClosedCount_ = 0;

                wetL = sanitize(wetL) * gateSm_;
                wetR = sanitize(wetR) * gateSm_;
                meterPeak_ = std::max(meterPeak_, std::max(std::fabs(wetL), std::fabs(wetR)));
                outL[i] = dryL * dryG + wetL * wetG * 1.4f;
                outR[i] = dryR * dryG + wetR * wetG * 1.4f;
            }
        }
        // live level for the skin: wet output peak with a short fall time, -60..0 dB mapped to 0..1
        meterEnv_ = std::max(meterPeak_, meterEnv_ * 0.88f);
        meterPeak_ = 0.0f;
        const float db = 20.0f * std::log10(meterEnv_ + 1.0e-6f);
        setNormalised(kMeter, mpc::clamp((db + 60.0f) * (1.0f / 60.0f), 0.0f, 1.0f));
        (void)gate_;
    }

private:
    static constexpr int kDisp = 12;
    static constexpr float kInK = 1.9f;

    void snapControls()
    {
        const int type = mpc::clamp(getInt(kType), 0, (int)kNumTypes - 1);
        const TypeInfo& ti = kTypes[type];
        size_.snap(ti.size);
        t60_.snap(mpc::clamp(0.3f * std::pow(20.0f, get(kDecay) * 0.01f) * ti.decay, 0.1f, 14.0f));
        damp_.snap(cutoffFor(type, get(kLiveliness) * 0.01f, sampleRate()));
        swirlDepth_.snap(16.0f * get(kSwirl) * 0.01f);
        swirlRate_.snap(0.07f * std::pow(10.0f, get(kSwirlRate) * 0.01f));
        warm_.snap(get(kWarmth) * 0.01f);
        air_.snap(get(kAir) * 0.01f);
        level_.snap(get(kLevel) * 0.01f);
        gateSm_ = 1.0f;
    }

    void clearTank()
    {
        for (int i = 0; i < kLines; ++i) { line_[i].reset(); lp_[i].reset(); }
        for (int c = 0; c < 2; ++c) { shim_[c].reset(); shim2_[c].reset(); shim3_[c].reset(); swirl_[c].reset(); rev_[c].reset(); for (int k = 0; k < 4; ++k) diff_[c][k].reset(); }
        for (int k = 0; k < kDisp; ++k) disp_[k].reset();
        spring_.reset(); springLp_.reset(); springFb_ = 0.0f;
        shimOut_[0] = shimOut_[1] = 0.0f;
        gateClosedCount_ = 0;
    }

    float srScale_ = 1.0f;
    DelayLine line_[kLines];
    OnePoleLP lp_[kLines];
    float phase_[kLines] = {};
    Allpass diff_[2][4];
    Allpass disp_[kDisp];
    DelayLine spring_;
    OnePoleLP springLp_;
    float springFb_ = 0.0f;
    OctaveUp shim_[2], shim2_[2], shim3_[2];
    OnePoleLP warmLp_[2];
    Smoother swirlDepth_, swirlRate_, warm_, air_;
    OnePoleLP airLp_[2];
    float meterPeak_ = 0.0f, meterEnv_ = 0.0f;
    DelayLine swirl_[2];
    float swirlPhase_[2] = { 0.0f, 0.25f };
    float shimOut_[2] = { 0.0f, 0.0f };
    ReverseGrains rev_[2];
    Smoother size_, t60_, damp_, level_, gate_;
    float env_ = 0.0f;
    int hold_ = 0;
    float gateSm_ = 1.0f;
    float gateCoeff_ = 0.0007f;
    int gateClosedCount_ = 0;
};

MPC_PLUGIN_ENTRY(TidalVerb)

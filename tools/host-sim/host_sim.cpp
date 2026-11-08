// host_sim.cpp - a tiny VST2 host that behaves like the Akai MPC's plugin host.
//
// SPDX-License-Identifier: MIT
//
// Loads a NATIVELY built plugin (.dylib / .so / .dll), talks to it exactly the
// way the MPC firmware does (same opcode order, same quirks), renders some
// audio, and reports PASS / WARN / FAIL lines so a plugin can be tested
// without hardware.
//
//   host_sim <lib> dump                       -> params.json on stdout
//   host_sim <lib> test [options]             -> readable test report
//
// Single file, C++17, no dependencies beyond the standard library and
// dlopen (POSIX) / LoadLibrary (Windows).  Build with:
//   c++ -std=c++17 -O2 -o host_sim host_sim.cpp
//
// The VST2 ABI comes from the SDK's own clean-room header; no Steinberg code.

#if defined(__has_include)
  #if __has_include("mpc_vst2_abi.h")
    #include "mpc_vst2_abi.h"
  #else
    #include "../../framework/mpc_vst2_abi.h"
  #endif
#else
  #include "../../framework/mpc_vst2_abi.h"
#endif

#include <algorithm>
#include <cctype>
#include <chrono>
#include <cmath>
#include <csignal>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <functional>
#include <random>
#include <set>
#include <string>
#include <vector>

#ifndef M_PI
  #define M_PI 3.14159265358979323846
#endif

#if defined(_WIN32)
  #ifndef WIN32_LEAN_AND_MEAN
    #define WIN32_LEAN_AND_MEAN
  #endif
  #include <windows.h>
  #include <io.h>
#else
  #include <dlfcn.h>
  #include <unistd.h>
#endif

// ===========================================================================
// Opcode names (for --verbose)
// ===========================================================================

static const char* kEffNames[] = {
    "effOpen", "effClose", "effSetProgram", "effGetProgram", "effSetProgramName", "effGetProgramName",
    "effGetParamLabel", "effGetParamDisplay", "effGetParamName", "effGetVu(deprecated)", "effSetSampleRate",
    "effSetBlockSize", "effMainsChanged", "effEditGetRect", "effEditOpen", "effEditClose",
    "effEditDraw(deprecated)", "effEditMouse(deprecated)", "effEditKey(deprecated)", "effEditIdle",
    "effEditTop(deprecated)", "effEditSleep(deprecated)", "effIdentify", "effGetChunk", "effSetChunk",
    "effProcessEvents", "effCanBeAutomated", "effString2Parameter", "effGetNumProgramCategories(deprecated)",
    "effGetProgramNameIndexed", "effCopyProgram(deprecated)", "effConnectInput(deprecated)",
    "effConnectOutput(deprecated)", "effGetInputProperties", "effGetOutputProperties", "effGetPlugCategory",
    "effGetCurrentPosition(deprecated)", "effGetDestinationBuffer(deprecated)", "effOfflineNotify",
    "effOfflinePrepare", "effOfflineRun", "effProcessVarIo", "effSetSpeakerArrangement",
    "effSetBlockSizeAndSampleRate(deprecated)", "effSetBypass", "effGetEffectName", "effGetErrorText(deprecated)",
    "effGetVendorString", "effGetProductString", "effGetVendorVersion", "effVendorSpecific", "effCanDo",
    "effGetTailSize", "effIdle(deprecated)", "effGetIcon(deprecated)", "effSetViewPosition(deprecated)",
    "effGetParameterProperties", "effKeysRequired(deprecated)", "effGetVstVersion", "effEditKeyDown",
    "effEditKeyUp", "effSetEditKnobMode", "effGetMidiProgramName", "effGetCurrentMidiProgram",
    "effGetMidiProgramCategory", "effHasMidiProgramsChanged", "effGetMidiKeyName", "effBeginSetProgram",
    "effEndSetProgram", "effGetSpeakerArrangement", "effShellGetNextPlugin", "effStartProcess",
    "effStopProcess", "effSetTotalSampleToProcess", "effSetPanLaw", "effBeginLoadBank", "effBeginLoadProgram",
    "effSetProcessPrecision", "effGetNumMidiInputChannels", "effGetNumMidiOutputChannels"
};

static const char* kMasterNames[] = {
    "audioMasterAutomate", "audioMasterVersion", "audioMasterCurrentId", "audioMasterIdle",
    "audioMasterPinConnected(deprecated)", "audioMaster(5)", "audioMasterWantMidi(deprecated)",
    "audioMasterGetTime", "audioMasterProcessEvents", "audioMasterSetTime(deprecated)",
    "audioMasterTempoAt(deprecated)", "audioMasterGetNumAutomatableParameters(deprecated)",
    "audioMasterGetParameterQuantization(deprecated)", "audioMasterIOChanged", "audioMasterNeedIdle(deprecated)",
    "audioMasterSizeWindow", "audioMasterGetSampleRate", "audioMasterGetBlockSize", "audioMasterGetInputLatency",
    "audioMasterGetOutputLatency", "audioMasterGetPreviousPlug(deprecated)", "audioMasterGetNextPlug(deprecated)",
    "audioMasterWillReplaceOrAccumulate(deprecated)", "audioMasterGetCurrentProcessLevel",
    "audioMasterGetAutomationState", "audioMasterOfflineStart", "audioMasterOfflineRead", "audioMasterOfflineWrite",
    "audioMasterOfflineGetCurrentPass", "audioMasterOfflineGetCurrentMetaPass",
    "audioMasterSetOutputSampleRate(deprecated)", "audioMasterGetOutputSpeakerArrangement(deprecated)",
    "audioMasterGetVendorString", "audioMasterGetProductString", "audioMasterGetVendorVersion",
    "audioMasterVendorSpecific", "audioMasterSetIcon(deprecated)", "audioMasterCanDo", "audioMasterGetLanguage",
    "audioMasterOpenWindow(deprecated)", "audioMasterCloseWindow(deprecated)", "audioMasterGetDirectory",
    "audioMasterUpdateDisplay", "audioMasterBeginEdit", "audioMasterEndEdit", "audioMasterOpenFileSelector",
    "audioMasterCloseFileSelector", "audioMasterEditFile(deprecated)", "audioMasterGetChunkFile(deprecated)",
    "audioMasterGetInputSpeakerArrangement(deprecated)"
};

static std::string effName(int op)
{
    if (op >= 0 && op < (int)(sizeof(kEffNames) / sizeof(kEffNames[0]))) return kEffNames[op];
    return "eff(" + std::to_string(op) + ")";
}
static std::string masterName(int op)
{
    if (op >= 0 && op < (int)(sizeof(kMasterNames) / sizeof(kMasterNames[0]))) return kMasterNames[op];
    return "audioMaster(" + std::to_string(op) + ")";
}

// ===========================================================================
// Crash handling: say which phase was running, then exit 2
// ===========================================================================

static const char* g_phase = "startup";
static char g_lastCall[128] = "none";

static void setPhase(const char* p) { g_phase = p; }
static void setLastCall(const std::string& s)
{
    std::strncpy(g_lastCall, s.c_str(), sizeof(g_lastCall) - 1);
    g_lastCall[sizeof(g_lastCall) - 1] = 0;
}

static void crashHandler(int sig)
{
    const char* name = sig == SIGSEGV ? "SIGSEGV (bad memory access)"
                     : sig == SIGFPE  ? "SIGFPE (arithmetic fault)"
                     : sig == SIGILL  ? "SIGILL (illegal instruction)"
                     : sig == SIGABRT ? "SIGABRT (abort / assertion)"
                     : "signal";
    static char buf[512];
    std::snprintf(buf, sizeof(buf),
                  "\nCRASH  %s\n       phase: %s\n       last call into the plugin: %s\n"
                  "       (exit code 2)\n", name, g_phase, g_lastCall);
#if defined(_WIN32)
    std::fputs(buf, stderr);
    std::fflush(stderr);
    _exit(2);
#else
    ssize_t ignored = write(2, buf, std::strlen(buf));
    (void)ignored;
    _exit(2);
#endif
}

static void installCrashHandlers()
{
    std::signal(SIGSEGV, crashHandler);
    std::signal(SIGFPE, crashHandler);
    std::signal(SIGILL, crashHandler);
    std::signal(SIGABRT, crashHandler);
#ifdef SIGBUS
    std::signal(SIGBUS, crashHandler);
#endif
}

// ===========================================================================
// Host state and the audioMaster callback
// ===========================================================================

struct Host
{
    double sampleRate = 44100.0;
    int blockSize = 128;
    double samplePos = 0.0;
    bool playing = false;
    bool verbose = false;
    VstInt32 uid = 0;
    AEffect* effect = nullptr;
    int initialDelay = 0;
    long midiOutEvents = 0, midiOutCalls = 0;
    long automateCalls = 0, updateDisplayCalls = 0, ioChangedCalls = 0;
    long getTimeCalls = 0, unknownCalls = 0;
    std::set<int> unknownOpcodes;
    VstTimeInfo time{};
};
static Host g;

static void vlog(const char* fmt, ...)
{
    if (!g.verbose) return;
    va_list args;
    va_start(args, fmt);
    std::vfprintf(stderr, fmt, args);
    std::fputc('\n', stderr);
    va_end(args);
}

static void fillTimeInfo()
{
    const double tempo = 120.0;
    VstTimeInfo& t = g.time;
    std::memset(&t, 0, sizeof(t));
    t.samplePos = g.samplePos;
    t.sampleRate = g.sampleRate;
    t.nanoSeconds = (double)std::chrono::duration_cast<std::chrono::nanoseconds>(
                        std::chrono::steady_clock::now().time_since_epoch()).count();
    t.ppqPos = g.samplePos / g.sampleRate * tempo / 60.0;
    t.tempo = tempo;
    t.barStartPos = std::floor(t.ppqPos / 4.0) * 4.0;
    t.timeSigNumerator = 4;
    t.timeSigDenominator = 4;
    t.flags = kVstTempoValid | kVstTimeSigValid | kVstPpqPosValid | kVstBarsValid | kVstNanosValid;
    if (g.playing) t.flags |= kVstTransportPlaying;
}

static VstIntPtr hostCallback(AEffect* effect, VstInt32 opcode, VstInt32 index, VstIntPtr value, void* ptr, float opt)
{
    (void)effect;
    bool logIt = g.verbose && opcode != audioMasterGetTime;
    VstIntPtr result = 0;
    switch (opcode)
    {
        case audioMasterVersion: result = 2400; break;
        case audioMasterCurrentId: result = g.uid; break;
        case audioMasterGetVendorString:
        case audioMasterGetProductString:
            if (ptr) std::strcpy(static_cast<char*>(ptr), "MPC 3");
            result = 1; break;
        case audioMasterGetVendorVersion: result = 0x0101; break;
        case audioMasterGetSampleRate: result = (VstIntPtr)g.sampleRate; break;
        case audioMasterGetBlockSize: result = g.blockSize; break;
        case audioMasterGetTime:
            fillTimeInfo();
            if (g.verbose && g.getTimeCalls == 0)
                vlog("[plugin->host] audioMasterGetTime (logged once; called every block) -> VstTimeInfo tempo=120 4/4%s",
                     g.playing ? " playing" : " stopped");
            ++g.getTimeCalls;
            result = (VstIntPtr)&g.time; break;
        case audioMasterCanDo:
        {
            const char* q = static_cast<const char*>(ptr);
            if (q && (!std::strcmp(q, "sendVstEvents") || !std::strcmp(q, "sendVstMidiEvent") ||
                      !std::strcmp(q, "sendVstTimeInfo") || !std::strcmp(q, "receiveVstEvents") ||
                      !std::strcmp(q, "receiveVstMidiEvent") || !std::strcmp(q, "sizeWindow") ||
                      !std::strcmp(q, "supplyIdle")))
                result = 1;
            if (logIt) vlog("[plugin->host] audioMasterCanDo(\"%s\") -> %ld", q ? q : "(null)", (long)result);
            return result;
        }
        case audioMasterAutomate:
            ++g.automateCalls;
            if (logIt) vlog("[plugin->host] audioMasterAutomate(param %d, value %.4f)", (int)index, (double)opt);
            return 0;
        case audioMasterUpdateDisplay: ++g.updateDisplayCalls; result = 1; break;
        case audioMasterIOChanged:
            ++g.ioChangedCalls;
            if (g.effect) g.initialDelay = g.effect->initialDelay;
            if (logIt) vlog("[plugin->host] audioMasterIOChanged -> re-read initialDelay = %d", g.initialDelay);
            return 1;
        case audioMasterProcessEvents:
        {
            const VstEvents* ev = static_cast<const VstEvents*>(ptr);
            ++g.midiOutCalls;
            if (ev) g.midiOutEvents += ev->numEvents;
            if (logIt) vlog("[plugin->host] audioMasterProcessEvents(%d events)", ev ? (int)ev->numEvents : 0);
            return 1;
        }
        case audioMasterGetCurrentProcessLevel: result = 0; break;
        default:
            ++g.unknownCalls;
            g.unknownOpcodes.insert(opcode);
            result = 0;
            break;
    }
    if (logIt)
        vlog("[plugin->host] %s(index=%d, value=%ld, opt=%g) -> %ld", masterName(opcode).c_str(), (int)index,
             (long)value, (double)opt, (long)result);
    return result;
}

// ===========================================================================
// Report
// ===========================================================================

struct Report
{
    int passes = 0, warns = 0, fails = 0;
    bool print = true;   // dump mode prints only failures (to stderr)

    void line(const char* tag, const char* fmt, va_list args)
    {
        char buf[2048];
        std::vsnprintf(buf, sizeof(buf), fmt, args);
        FILE* out = print ? stdout : stderr;
        if (print || !std::strcmp(tag, "FAIL")) std::fprintf(out, "%-5s %s\n", tag, buf);
        if (print) std::fflush(out);
    }
    void pass(const char* fmt, ...) { ++passes; va_list a; va_start(a, fmt); line("PASS", fmt, a); va_end(a); }
    void warn(const char* fmt, ...) { ++warns;  va_list a; va_start(a, fmt); line("WARN", fmt, a); va_end(a); }
    void fail(const char* fmt, ...) { ++fails;  va_list a; va_start(a, fmt); line("FAIL", fmt, a); va_end(a); }
    void info(const char* fmt, ...) { va_list a; va_start(a, fmt); line("INFO", fmt, a); va_end(a); }
    void section(const char* title) { if (print) { std::printf("\n== %s ==\n", title); std::fflush(stdout); } }
};

// ===========================================================================
// Talking to the plugin
// ===========================================================================

static bool isStringOpcode(VstInt32 op)
{
    return op == effGetProgramName || op == effGetParamLabel || op == effGetParamDisplay || op == effGetParamName ||
           op == effGetProgramNameIndexed || op == effGetEffectName || op == effGetVendorString ||
           op == effGetProductString;
}

static VstIntPtr disp(AEffect* e, VstInt32 opcode, VstInt32 index = 0, VstIntPtr value = 0, void* ptr = nullptr, float opt = 0.0f)
{
    std::string name = effName(opcode);
    setLastCall(name);
    VstIntPtr r = e->dispatcher(e, opcode, index, value, ptr, opt);
    if (g.verbose)
    {
        if (opcode == effCanDo)
            vlog("[host->plugin] effCanDo(\"%s\") -> %ld", ptr ? static_cast<const char*>(ptr) : "(null)", (long)r);
        else if (isStringOpcode(opcode))
            vlog("[host->plugin] %s(index=%d) -> %ld \"%s\"", name.c_str(), (int)index, (long)r,
                 ptr ? static_cast<const char*>(ptr) : "");
        else if (opcode == effProcessEvents)
        {
            const VstEvents* ev = static_cast<const VstEvents*>(ptr);
            int n = ev ? (int)ev->numEvents : 0;
            if (n > 0) vlog("[host->plugin] effProcessEvents(%d events) -> %ld", n, (long)r);
        }
        else
            vlog("[host->plugin] %s(index=%d, value=%ld, ptr=%s, opt=%g) -> %ld", name.c_str(), (int)index,
                 (long)value, ptr ? "yes" : "null", (double)opt, (long)r);
    }
    return r;
}

static std::string getStr(AEffect* e, VstInt32 opcode, VstInt32 index = 0, VstIntPtr* ret = nullptr)
{
    char buf[1024];
    std::memset(buf, 0, sizeof(buf));
    VstIntPtr r = disp(e, opcode, index, 0, buf, 0.0f);
    buf[sizeof(buf) - 1] = 0;
    if (ret) *ret = r;
    return std::string(buf);
}

static float getParam(AEffect* e, int i)
{
    setLastCall("getParameter(" + std::to_string(i) + ")");
    return e->getParameter(e, i);
}
static void setParam(AEffect* e, int i, float v)
{
    setLastCall("setParameter(" + std::to_string(i) + ")");
    if (g.verbose) vlog("[host->plugin] setParameter(%d, %.5f)", i, (double)v);
    e->setParameter(e, i, v);
}

static std::string uidToString(VstInt32 uid)
{
    std::string s;
    for (int shift = 24; shift >= 0; shift -= 8)
    {
        char c = (char)((uid >> shift) & 0xff);
        s += (c >= 32 && c < 127) ? c : '?';
    }
    return s;
}

// ---------------------------------------------------------------------------
// Library loading
// ---------------------------------------------------------------------------

typedef AEffect* (*EntryFn)(audioMasterCallback);

struct Library
{
    void* handle = nullptr;
    EntryFn entry = nullptr;
    std::string error;
};

static Library openLibrary(const std::string& path)
{
    Library lib;
#if defined(_WIN32)
    HMODULE h = LoadLibraryA(path.c_str());
    if (!h) { lib.error = "LoadLibrary failed (error " + std::to_string((unsigned)GetLastError()) + ")"; return lib; }
    lib.handle = (void*)h;
    const char* names[] = { "VSTPluginMain", "main" };
    for (const char* n : names)
    {
        FARPROC p = GetProcAddress(h, n);
        if (p) { lib.entry = reinterpret_cast<EntryFn>(p); break; }
    }
#else
    void* h = dlopen(path.c_str(), RTLD_NOW | RTLD_LOCAL);
    if (!h) { const char* err = dlerror(); lib.error = err ? err : "dlopen failed"; return lib; }
    lib.handle = h;
    const char* names[] = { "VSTPluginMain", "main_macho", "main" };
    for (const char* n : names)
    {
        void* p = dlsym(h, n);
        if (p) { lib.entry = reinterpret_cast<EntryFn>(p); break; }
    }
#endif
    if (!lib.entry) lib.error = "no VSTPluginMain symbol exported (is this a VST2 plugin? did you use MPC_PLUGIN_ENTRY?)";
    return lib;
}

static void closeLibrary(Library& lib)
{
    if (!lib.handle) return;
#if defined(_WIN32)
    FreeLibrary((HMODULE)lib.handle);
#else
    dlclose(lib.handle);
#endif
    lib.handle = nullptr;
}

// ---------------------------------------------------------------------------
// A loaded plugin plus everything the MPC learns about it while loading
// ---------------------------------------------------------------------------

struct Plug
{
    Library lib;
    AEffect* e = nullptr;
    std::string path, name, vendor;
    VstIntPtr category = 0;
    bool isSynth = false, hasEditor = false, hasChunks = false;
    VstIntPtr canDoMidi = 0, canDoBypass = 0;
    std::vector<std::string> paramNames, programs;
    std::vector<bool> automatable;
    int initialDelay = 0;
};

static void makeArrangement(VstSpeakerArrangement& a, int channels)
{
    std::memset(&a, 0, sizeof(a));
    if (channels <= 0) { a.type = kSpeakerArrEmpty; a.numChannels = 0; }
    else if (channels == 1) { a.type = kSpeakerArrMono; a.numChannels = 1; }
    else { a.type = kSpeakerArrStereo; a.numChannels = 2; }
    for (int i = 0; i < a.numChannels; ++i) a.speakers[i].type = i;
}

// The MPC load sequence (as observed on the device). Returns false on FAIL.
static bool mpcLoad(Plug& p, Report& rep, double sr, int block)
{
    setPhase("loading the library");
    p.lib = openLibrary(p.path);
    if (!p.lib.entry)
    {
        rep.fail("load: %s: %s", p.path.c_str(), p.lib.error.c_str());
        return false;
    }
    rep.pass("load: library opened and VSTPluginMain found");

    setPhase("VSTPluginMain (plugin constructor)");
    setLastCall("VSTPluginMain");
    AEffect* e = p.lib.entry(&hostCallback);
    if (!e) { rep.fail("VSTPluginMain returned NULL (the plugin refused to load)"); return false; }
    p.e = e;
    g.effect = e;

    bool ok = true;
    if (e->magic != kEffectMagic) { rep.fail("AEffect.magic is 0x%08x, expected 'VstP' (0x56737450)", (unsigned)e->magic); ok = false; }
    if (e->resvd2 != 0) { rep.fail("AEffect.resvd2 must be 0 (the MPC rejects the plugin otherwise)"); ok = false; }
    if (e->object == nullptr) { rep.fail("AEffect.object is NULL (the MPC rejects the plugin)"); ok = false; }
    if (e->dispatcher == nullptr) { rep.fail("AEffect.dispatcher is NULL"); ok = false; }
    if (!ok) return false;
    rep.pass("AEffect: magic 'VstP', resvd2 == 0, object != NULL");

    g.uid = e->uniqueID;
    p.isSynth = (e->flags & effFlagsIsSynth) != 0;
    p.hasEditor = (e->flags & effFlagsHasEditor) != 0;
    p.hasChunks = (e->flags & effFlagsProgramChunks) != 0;

    if (!(e->flags & effFlagsCanReplacing) || e->processReplacing == nullptr)
    { rep.fail("plugin must set effFlagsCanReplacing and provide processReplacing"); return false; }
    if (e->setParameter == nullptr || e->getParameter == nullptr)
    { rep.fail("AEffect.setParameter / getParameter must not be NULL"); return false; }
    if (e->numOutputs <= 0)
    { rep.fail("numOutputs is %d: the MPC does not list plugins without audio outputs", (int)e->numOutputs); return false; }
    if (e->numParams < 0 || e->numInputs < 0 || e->numPrograms < 0)
    { rep.fail("negative numParams/numInputs/numPrograms"); return false; }
    if (e->numInputs > 8 || e->numOutputs > 16)
        rep.warn("unusual channel count (%d in / %d out); host-sim sends a stereo arrangement like the MPC does",
                 (int)e->numInputs, (int)e->numOutputs);
    if (p.isSynth && e->numInputs > 0)
        rep.info("instrument declares %d audio inputs; the MPC feeds silence to instrument inputs", (int)e->numInputs);
    rep.info("uid '%s' (%d)  version %d  flags 0x%x  %s  %d in / %d out  %d params  %d programs%s%s",
             uidToString(e->uniqueID).c_str(), (int)e->uniqueID, (int)e->version, (unsigned)e->flags,
             p.isSynth ? "instrument" : "effect", (int)e->numInputs, (int)e->numOutputs, (int)e->numParams,
             (int)e->numPrograms, p.hasChunks ? "  chunks" : "  (no chunk state)", p.hasEditor ? "  editor" : "");

    setPhase("MPC load sequence (first identification)");
    g.sampleRate = sr;
    disp(e, effIdentify_Deprecated);
    disp(e, effSetSampleRate, 0, 0, nullptr, (float)sr);
    g.blockSize = std::max(32, block);
    disp(e, effSetBlockSize, 0, g.blockSize);
    disp(e, effOpen);

    {
        VstSpeakerArrangement* in = nullptr; VstSpeakerArrangement* out = nullptr;
        disp(e, effGetSpeakerArrangement, 0, (VstIntPtr)&in, &out);
    }
    for (int i = 0; i < e->numInputs; ++i)
    {
        VstPinProperties pin; std::memset(&pin, 0, sizeof(pin));
        disp(e, effGetInputProperties, i, 0, &pin);
    }
    for (int i = 0; i < e->numOutputs; ++i)
    {
        VstPinProperties pin; std::memset(&pin, 0, sizeof(pin));
        disp(e, effGetOutputProperties, i, 0, &pin);
    }
    p.automatable.clear();
    for (int i = 0; i < e->numParams; ++i) p.automatable.push_back(disp(e, effCanBeAutomated, i) != 0);
    p.canDoBypass = disp(e, effCanDo, 0, 0, (void*)"bypass");

    setPhase("MPC load sequence (second identification)");
    disp(e, effIdentify_Deprecated);
    disp(e, effSetSampleRate, 0, 0, nullptr, (float)sr);
    g.blockSize = std::max(128, block);
    disp(e, effSetBlockSize, 0, g.blockSize);
    p.vendor = getStr(e, effGetVendorString);
    disp(e, effOpen);   // yes, the MPC opens twice
    disp(e, effSetProgram, 0, 0);
    p.category = disp(e, effGetPlugCategory);

    p.programs.clear();
    int numPrograms = std::max(1, (int)e->numPrograms);
    VstIntPtr r = 0;
    std::string first = getStr(e, effGetProgramNameIndexed, 0, &r);
    if (r != 0)
    {
        p.programs.push_back(first);
        for (int i = 1; i < numPrograms; ++i)
        {
            VstIntPtr ri = 0;
            std::string s = getStr(e, effGetProgramNameIndexed, i, &ri);
            p.programs.push_back(ri != 0 ? s : std::string());
        }
    }
    else
    {
        for (int i = 0; i < numPrograms; ++i)
        {
            disp(e, effSetProgram, 0, i);
            p.programs.push_back(getStr(e, effGetProgramName));
        }
        disp(e, effSetProgram, 0, 0);
    }
    p.canDoMidi = disp(e, effCanDo, 0, 0, (void*)"receiveVstMidiEvent");
    p.initialDelay = e->initialDelay;
    g.initialDelay = p.initialDelay;
    p.name = getStr(e, effGetEffectName);

    p.paramNames.clear();
    for (int i = 0; i < e->numParams; ++i) p.paramNames.push_back(getStr(e, effGetParamName, i));

    if (p.name.empty()) rep.warn("effGetEffectName returned an empty name");
    if (p.vendor.empty()) rep.warn("effGetVendorString returned an empty vendor (skins are looked up by vendor + name)");
    if (p.name.size() > kVstMaxEffectNameLen) rep.warn("effect name is longer than %d characters", (int)kVstMaxEffectNameLen);
    if (p.vendor.size() > kVstMaxVendorStrLen) rep.warn("vendor string is longer than %d characters", (int)kVstMaxVendorStrLen);
    for (int i = 0; i < e->numParams; ++i)
        if (p.paramNames[(size_t)i].empty()) rep.warn("parameter %d has an empty name", i);
    if (p.isSynth && p.canDoMidi != 1)
        rep.warn("instrument answers %ld to effCanDo \"receiveVstMidiEvent\" (the MPC expects 1)", (long)p.canDoMidi);
    if (!p.isSynth && p.canDoMidi == 1)
        rep.info("effect says it receives MIDI; note that the MPC never sends MIDI to effects");
    if (p.isSynth && p.category != kPlugCategSynth)
        rep.warn("instrument reports plug category %ld (expected kPlugCategSynth = 2)", (long)p.category);
    rep.pass("MPC load sequence: \"%s\" by \"%s\", %s, %d program(s), first program \"%s\"",
             p.name.c_str(), p.vendor.c_str(), p.isSynth ? "instrument" : "effect", numPrograms,
             p.programs.empty() ? "" : p.programs[0].c_str());
    return true;
}

// "parameter 0 toggle quirk": on every prepare the MPC, when the plugin has
// no editor, sets parameter 0 to 1.0 or 0.0 and then back.
static void param0Quirk(Plug& p)
{
    if (p.hasEditor || p.e->numParams <= 0) return;
    float old = getParam(p.e, 0);
    setParam(p.e, 0, old < 0.5f ? 1.0f : 0.0f);
    setParam(p.e, 0, old);
}

// The MPC prepare sequence (first time).
static void mpcPrepare(Plug& p, double sr, int block)
{
    setPhase("prepare (speaker arrangement, sample rate, block size, resume)");
    AEffect* e = p.e;
    VstSpeakerArrangement in, out;
    makeArrangement(in, e->numInputs);
    makeArrangement(out, e->numOutputs);
    disp(e, effSetSpeakerArrangement, 0, (VstIntPtr)&in, &out);
    g.sampleRate = sr;
    disp(e, effSetSampleRate, 0, 0, nullptr, (float)sr);
    g.blockSize = std::max(128, block);
    disp(e, effSetBlockSize, 0, g.blockSize);
    disp(e, effMainsChanged, 0, 1);
    param0Quirk(p);
    disp(e, effStartProcess);
}

// Re-prepare, as the MPC does when the block size changes.
static void mpcReprepare(Plug& p, int block)
{
    setPhase("re-prepare (suspend, stop, set block size, resume, start)");
    AEffect* e = p.e;
    disp(e, effMainsChanged, 0, 0);
    disp(e, effStopProcess);
    g.blockSize = std::max(128, block);
    disp(e, effSetBlockSize, 0, g.blockSize);
    disp(e, effMainsChanged, 0, 1);
    param0Quirk(p);
    disp(e, effStartProcess);
}

static void mpcClose(Plug& p)
{
    if (!p.e) return;
    setPhase("teardown (stop, suspend, close)");
    disp(p.e, effStopProcess);
    disp(p.e, effMainsChanged, 0, 0);
    disp(p.e, effClose);   // the plugin frees itself here; never touch p.e again
    p.e = nullptr;
    g.effect = nullptr;
    closeLibrary(p.lib);
}

// ===========================================================================
// Parameter kind inference (for dump)
// ===========================================================================

struct ParamKind
{
    std::string kind = "float";
    std::vector<std::string> choices;
};

static std::string trimmed(const std::string& s)
{
    size_t a = s.find_first_not_of(" \t\r\n");
    size_t b = s.find_last_not_of(" \t\r\n");
    return a == std::string::npos ? std::string() : s.substr(a, b - a + 1);
}

static bool isIntegerString(const std::string& in, long* value = nullptr)
{
    std::string s = trimmed(in);
    if (s.empty()) return false;
    size_t i = (s[0] == '-' || s[0] == '+') ? 1 : 0;
    if (i >= s.size()) return false;
    for (size_t k = i; k < s.size(); ++k) if (!std::isdigit((unsigned char)s[k])) return false;
    if (value) *value = std::strtol(s.c_str(), nullptr, 10);
    return true;
}

static bool isNumberString(const std::string& in)
{
    std::string s = trimmed(in);
    if (s.empty()) return false;
    char* end = nullptr;
    std::strtod(s.c_str(), &end);
    return end && *end == '\0';
}

static std::string lower(std::string s) { for (char& c : s) c = (char)std::tolower((unsigned char)c); return s; }

static bool isBoolPair(const std::string& a, const std::string& b)
{
    static const char* pairs[][2] = { {"off", "on"}, {"no", "yes"}, {"false", "true"}, {"disabled", "enabled"},
                                      {"inactive", "active"}, {"0", "1"} };
    std::string la = lower(trimmed(a)), lb = lower(trimmed(b));
    for (auto& pr : pairs) if (la == pr[0] && lb == pr[1]) return true;
    return false;
}

static std::vector<std::string> probeDisplays(AEffect* e, int i, int steps)
{
    std::vector<std::string> out;
    for (int k = 0; k < steps; ++k)
    {
        setParam(e, i, (float)k / (float)(steps - 1));
        out.push_back(getStr(e, effGetParamDisplay, i));
    }
    return out;
}

static std::vector<std::string> orderedDistinct(const std::vector<std::string>& v)
{
    std::vector<std::string> out;
    for (const std::string& s : v) if (std::find(out.begin(), out.end(), s) == out.end()) out.push_back(s);
    return out;
}

static ParamKind inferParamKind(AEffect* e, int i)
{
    ParamKind pk;
    float original = getParam(e, i);

    std::vector<std::string> fine32 = probeDisplays(e, i, 32);
    std::vector<std::string> fine64 = probeDisplays(e, i, 64);
    std::set<std::string> set32(fine32.begin(), fine32.end());
    std::set<std::string> set64(fine64.begin(), fine64.end());

    bool discrete = false;
    std::vector<std::string> choices;
    for (int n = 2; n <= 16 && !discrete; ++n)
    {
        std::vector<std::string> d = probeDisplays(e, i, n);
        std::set<std::string> sn(d.begin(), d.end());
        if ((int)sn.size() == n && sn == set32 && sn == set64) { discrete = true; choices = d; }
    }
    if (!discrete && set64.size() > 16 && set64.size() <= 64)
    {
        // more than 16 choices: compare two fine probes with each other
        std::vector<std::string> fine128 = probeDisplays(e, i, 128);
        std::set<std::string> set128(fine128.begin(), fine128.end());
        if (set128 == set64) { discrete = true; choices = orderedDistinct(fine128); }
    }

    setParam(e, i, original);

    if (discrete)
    {
        bool allInts = true, consecutive = true;
        long prev = 0;
        for (size_t k = 0; k < choices.size(); ++k)
        {
            long v = 0;
            if (!isIntegerString(choices[k], &v)) { allInts = false; break; }
            if (k > 0 && v != prev + 1) consecutive = false;
            prev = v;
        }
        bool allNumbers = true;
        for (const std::string& s : choices) if (!isNumberString(s)) { allNumbers = false; break; }

        if (choices.size() == 2 && isBoolPair(choices[0], choices[1])) { pk.kind = "bool"; pk.choices = choices; }
        else if (allInts && consecutive) { pk.kind = "int"; }
        else if (allNumbers && !allInts) { pk.kind = "float"; }   // e.g. a 1-decimal float with a tiny range
        else { pk.kind = "choice"; pk.choices = choices; }
        return pk;
    }

    bool allInts = true;
    for (const std::string& s : fine64) if (!isIntegerString(s)) { allInts = false; break; }
    pk.kind = (allInts && set64.size() > 1) ? "int" : "float";
    return pk;
}

// ===========================================================================
// JSON helpers
// ===========================================================================

static std::string jsonStr(const std::string& s)
{
    std::string out = "\"";
    for (unsigned char c : s)
    {
        switch (c)
        {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (c < 0x20) { char b[8]; std::snprintf(b, sizeof(b), "\\u%04x", c); out += b; }
                else out += (char)c;
        }
    }
    return out + "\"";
}

static std::string jsonFloat(double v)
{
    if (std::isnan(v) || std::isinf(v)) return "0";
    char b[64];
    std::snprintf(b, sizeof(b), "%.6g", v);
    std::string s = b;
    if (s.find_first_of(".eE") == std::string::npos) s += ".0";
    return s;
}

static std::string jsonStringArray(const std::vector<std::string>& v)
{
    std::string out = "[";
    for (size_t i = 0; i < v.size(); ++i) { if (i) out += ", "; out += jsonStr(v[i]); }
    return out + "]";
}

// ===========================================================================
// dump
// ===========================================================================

static int runDump(const std::string& path)
{
    Report rep;
    rep.print = false;
    Plug p;
    p.path = path;
    if (!mpcLoad(p, rep, 44100.0, 128))
    {
        std::fprintf(stderr, "host_sim: could not load %s (see FAIL lines above)\n", path.c_str());
        return 1;
    }
    AEffect* e = p.e;

    struct Entry { std::string name, label, display; float def; bool automatable; ParamKind kind; };
    std::vector<Entry> entries;
    setPhase("dump: reading parameters");
    for (int i = 0; i < e->numParams; ++i)
    {
        Entry en;
        en.name = p.paramNames[(size_t)i];
        en.label = getStr(e, effGetParamLabel, i);
        en.display = getStr(e, effGetParamDisplay, i);
        en.def = getParam(e, i);
        en.automatable = p.automatable[(size_t)i];
        entries.push_back(en);
    }
    setPhase("dump: probing parameter kinds");
    for (int i = 0; i < e->numParams; ++i) entries[(size_t)i].kind = inferParamKind(e, i);

    std::string out;
    out += "{\n";
    out += "  \"name\": " + jsonStr(p.name) + ", \"vendor\": " + jsonStr(p.vendor) +
           ", \"uid\": " + jsonStr(uidToString(e->uniqueID)) + ", \"uidInt\": " + std::to_string((long long)e->uniqueID) +
           ", \"version\": " + std::to_string((long)e->version) + ",\n";
    out += std::string("  \"isInstrument\": ") + (p.isSynth ? "true" : "false") +
           ", \"numInputs\": " + std::to_string((long)e->numInputs) +
           ", \"numOutputs\": " + std::to_string((long)e->numOutputs) +
           ", \"numPrograms\": " + std::to_string((long)std::max(1, (int)e->numPrograms)) + ",\n";
    out += "  \"programs\": " + jsonStringArray(p.programs) + ",\n";
    out += "  \"params\": [";
    for (size_t i = 0; i < entries.size(); ++i)
    {
        const Entry& en = entries[i];
        out += i ? ",\n    " : "\n    ";
        out += "{\"index\": " + std::to_string(i) + ", \"id\": null, \"name\": " + jsonStr(en.name) +
               ", \"label\": " + jsonStr(en.label) + ", \"display\": " + jsonStr(en.display) +
               ", \"default\": " + jsonFloat(en.def) + ", \"automatable\": " + (en.automatable ? "true" : "false") +
               ", \"kind\": " + jsonStr(en.kind.kind) + ", \"choices\": " + jsonStringArray(en.kind.choices) + "}";
    }
    out += entries.empty() ? "]\n" : "\n  ]\n";
    out += "}\n";

    mpcClose(p);
    std::fputs(out.c_str(), stdout);
    std::fflush(stdout);
    return rep.fails ? 1 : 0;
}

// ===========================================================================
// test
// ===========================================================================

struct AudioStats
{
    double peak = 0.0, sumSq = 0.0;
    long samples = 0, nans = 0, infs = 0, clips = 0, denormals = 0;
    void add(const float* buf, int n)
    {
        for (int i = 0; i < n; ++i)
        {
            float x = buf[i];
            if (std::isnan(x)) { ++nans; continue; }
            if (std::isinf(x)) { ++infs; continue; }
            float a = std::fabs(x);
            if (a > 1.0f) ++clips;
            if (a < 1e-30f && x != 0.0f) ++denormals;
            if (a > peak) peak = a;
            sumSq += (double)x * x;
        }
        samples += n;
    }
    void add(const AudioStats& o)
    {
        peak = std::max(peak, o.peak); sumSq += o.sumSq; samples += o.samples;
        nans += o.nans; infs += o.infs; clips += o.clips; denormals += o.denormals;
    }
    double rms() const { return samples ? std::sqrt(sumSq / (double)samples) : 0.0; }
};

static double toDb(double lin) { return lin > 0 ? 20.0 * std::log10(lin) : -200.0; }

struct MidiEv { long frame; uint8_t b0, b1, b2; };

struct PassResult
{
    AudioStats all, tail;      // tail = last 100 ms of the pass
    double cpuAvg = 0, cpuMax = 0;
    long blocks = 0, frames = 0;
};

struct Renderer
{
    Plug& p;
    Report& rep;
    int numIn, numOut, cap;
    std::vector<std::vector<float>> inBufs, outBufs, sharedBufs;
    std::vector<float*> inPtrs, outPtrs, sharedIn, sharedOut;
    std::vector<int16_t>* wav = nullptr;
    double sinePhase = 0.0;
    std::mt19937 noiseRng{12345};
    bool eventsEveryBlock;

    Renderer(Plug& plug, Report& r) : p(plug), rep(r)
    {
        numIn = p.e->numInputs; numOut = p.e->numOutputs;
        cap = 4096;
        inBufs.assign((size_t)std::max(numIn, 1), std::vector<float>((size_t)cap, 0.0f));
        outBufs.assign((size_t)numOut, std::vector<float>((size_t)cap, 0.0f));
        int shared = std::max(numIn, numOut);
        sharedBufs.assign((size_t)shared, std::vector<float>((size_t)cap, 0.0f));
        for (auto& b : inBufs) inPtrs.push_back(b.data());
        for (auto& b : outBufs) outPtrs.push_back(b.data());
        for (auto& b : sharedBufs) { sharedIn.push_back(b.data()); sharedOut.push_back(b.data()); }
        eventsEveryBlock = p.isSynth || p.canDoMidi == 1;
    }

    // Fills `frames` samples of test signal into the given channel buffers.
    void fillInput(std::vector<float*>& bufs, int chans, int frames, bool signal, double passPos, double passLen)
    {
        if (chans <= 0) return;
        float* first = bufs[0];
        for (int i = 0; i < frames; ++i)
        {
            float v = 0.0f;
            if (signal)
            {
                double t = passPos + (double)i / g.sampleRate;
                v = 0.251f * (float)std::sin(sinePhase);                     // -12 dBFS, 220 Hz
                sinePhase += 2.0 * M_PI * 220.0 / g.sampleRate;
                if (sinePhase > 2.0 * M_PI) sinePhase -= 2.0 * M_PI;
                double burstStart = passLen >= 1.0 ? 0.5 : passLen * 0.3;   // short noise burst
                if (t >= burstStart && t < burstStart + 0.05)
                    v += 0.3f * ((float)(noiseRng() & 0xffff) / 32768.0f - 1.0f);
            }
            first[i] = v;
        }
        for (int c = 1; c < chans; ++c) std::memcpy(bufs[(size_t)c], first, sizeof(float) * (size_t)frames);
    }

    PassResult run(const char* phase, double seconds, int block, const std::vector<MidiEv>& events,
                   bool signal, bool inPlace, bool capture, const std::function<void(long)>& betweenBlocks)
    {
        PassResult res;
        setPhase(phase);
        AEffect* e = p.e;
        long total = (long)std::llround(seconds * g.sampleRate);
        long tailStart = total - (long)std::llround(0.1 * g.sampleRate);
        long pos = 0;
        double cpuSum = 0; long cpuBlocks = 0;
        std::vector<VstMidiEvent> midi;
        std::vector<char> evStorage;
        std::vector<float*>& ins = inPlace ? sharedIn : inPtrs;
        std::vector<float*>& outs = inPlace ? sharedOut : outPtrs;
        int fillChans = inPlace ? (int)sharedBufs.size() : numIn;

        while (pos < total)
        {
            int frames = (int)std::min<long>(block, total - pos);
            if (betweenBlocks) betweenBlocks(res.blocks);

            fillInput(ins, fillChans, frames, signal && numIn > 0, (double)pos / g.sampleRate, seconds);
            if (inPlace) for (int c = numIn; c < fillChans; ++c) std::memset(outs[(size_t)c], 0, sizeof(float) * (size_t)frames);
            if (!inPlace) for (int c = 0; c < numOut; ++c) std::fill(outPtrs[(size_t)c], outPtrs[(size_t)c] + frames, 0.0f);

            // MIDI for this block
            if (eventsEveryBlock)
            {
                midi.clear();
                for (const MidiEv& ev : events)
                    if (ev.frame >= pos && ev.frame < pos + frames)
                    {
                        VstMidiEvent m; std::memset(&m, 0, sizeof(m));
                        m.type = kVstMidiType; m.byteSize = sizeof(VstMidiEvent);
                        m.deltaFrames = (VstInt32)(ev.frame - pos);
                        m.flags = kVstMidiEventIsRealtime;
                        m.midiData[0] = (char)ev.b0; m.midiData[1] = (char)ev.b1; m.midiData[2] = (char)ev.b2;
                        midi.push_back(m);
                    }
                evStorage.assign(sizeof(VstEvents) + sizeof(VstEvent*) * (midi.size() + 2), 0);
                VstEvents* evs = reinterpret_cast<VstEvents*>(evStorage.data());
                evs->numEvents = (VstInt32)midi.size();
                evs->reserved = 0;
                for (size_t i = 0; i < midi.size(); ++i) evs->events[i] = reinterpret_cast<VstEvent*>(&midi[i]);
                disp(e, effProcessEvents, 0, 0, evs);
            }

            fillTimeInfo();
            setLastCall("processReplacing(" + std::to_string(frames) + " frames)");
            auto t0 = std::chrono::steady_clock::now();
            e->processReplacing(e, ins.data(), outs.data(), frames);
            auto t1 = std::chrono::steady_clock::now();
            double elapsed = std::chrono::duration<double>(t1 - t0).count();
            if (frames == block)
            {
                double cpu = elapsed / ((double)frames / g.sampleRate) * 100.0;
                cpuSum += cpu; ++cpuBlocks; res.cpuMax = std::max(res.cpuMax, cpu);
            }

            AudioStats blockStats;
            for (int c = 0; c < numOut; ++c) blockStats.add(outs[(size_t)c], frames);
            res.all.add(blockStats);
            if (pos >= tailStart) res.tail.add(blockStats);

            if (capture && wav)
            {
                const float* l = outs[0];
                const float* r = outs[(size_t)std::min(1, numOut - 1)];
                for (int i = 0; i < frames; ++i)
                {
                    float a = std::max(-1.0f, std::min(1.0f, std::isfinite(l[i]) ? l[i] : 0.0f));
                    float b = std::max(-1.0f, std::min(1.0f, std::isfinite(r[i]) ? r[i] : 0.0f));
                    wav->push_back((int16_t)std::lround(a * 32767.0f));
                    wav->push_back((int16_t)std::lround(b * 32767.0f));
                }
            }

            g.samplePos += frames;
            pos += frames;
            ++res.blocks;
        }
        res.frames = total;
        res.cpuAvg = cpuBlocks ? cpuSum / (double)cpuBlocks : 0.0;
        return res;
    }
};

static std::vector<MidiEv> noteEvents(const std::vector<int>& notes, double onAt, double offAt)
{
    std::vector<MidiEv> ev;
    for (int n : notes)
    {
        ev.push_back({ (long)std::llround(onAt * g.sampleRate), 0x90, (uint8_t)n, 100 });
        ev.push_back({ (long)std::llround(offAt * g.sampleRate), 0x80, (uint8_t)n, 0 });
    }
    return ev;
}

static void checkAudio(Report& rep, const char* what, const AudioStats& s, bool clipIsInfo = false)
{
    if (s.nans || s.infs) rep.fail("%s: output contains %ld NaN and %ld Inf samples", what, s.nans, s.infs);
    if (s.clips && clipIsInfo) rep.info("%s: %ld samples above 0 dBFS (peak %.2f dBFS) - expected with random parameter values", what, s.clips, toDb(s.peak));
    else if (s.clips) rep.warn("%s: clipping - %ld samples with |x| > 1.0 (peak %.2f dBFS)", what, s.clips, toDb(s.peak));
    if (s.denormals) rep.warn("%s: %ld denormal samples (|x| < 1e-30) - flush tiny values to zero in feedback paths", what, s.denormals);
}

static bool writeWav(const std::string& path, const std::vector<int16_t>& data, int sampleRate, int channels)
{
    FILE* f = std::fopen(path.c_str(), "wb");
    if (!f) return false;
    auto u32 = [&](uint32_t v) { for (int i = 0; i < 4; ++i) std::fputc((v >> (8 * i)) & 0xff, f); };
    auto u16 = [&](uint16_t v) { for (int i = 0; i < 2; ++i) std::fputc((v >> (8 * i)) & 0xff, f); };
    uint32_t bytes = (uint32_t)(data.size() * 2);
    std::fwrite("RIFF", 1, 4, f); u32(36 + bytes); std::fwrite("WAVE", 1, 4, f);
    std::fwrite("fmt ", 1, 4, f); u32(16); u16(1); u16((uint16_t)channels); u32((uint32_t)sampleRate);
    u32((uint32_t)(sampleRate * channels * 2)); u16((uint16_t)(channels * 2)); u16(16);
    std::fwrite("data", 1, 4, f); u32(bytes);
    for (int16_t s : data) u16((uint16_t)s);
    std::fclose(f);
    return true;
}

struct TestOptions
{
    double seconds = 2.0;
    double sr = 44100.0;
    int block = 128;
    std::string wav;
    std::vector<int> notes{60};
    bool verbose = false, playing = false;
    unsigned seed = 1234;
};

static int runTest(const std::string& path, const TestOptions& opt)
{
    Report rep;
    g.verbose = opt.verbose;
    g.playing = opt.playing;
    g.sampleRate = opt.sr;
    g.blockSize = opt.block;

    std::printf("host_sim: testing %s\n", path.c_str());
    std::printf("          %.2f s at %g Hz, process block %d frames, notes", opt.seconds, opt.sr, opt.block);
    for (int n : opt.notes) std::printf(" %d", n);
    std::printf(", transport %s\n", opt.playing ? "playing" : "stopped");

    rep.section("Load (MPC sequence)");
    Plug p;
    p.path = path;
    if (!mpcLoad(p, rep, opt.sr, opt.block))
    {
        std::printf("\nResult: FAIL (the plugin could not be loaded)\n");
        return 1;
    }
    AEffect* e = p.e;
    const int numParams = e->numParams;

    rep.section("Prepare");
    mpcPrepare(p, opt.sr, opt.block);
    rep.pass("prepare: speaker arrangement, sample rate %g, block size %d, resume, %s, start",
             opt.sr, g.blockSize, p.hasEditor ? "no param-0 toggle (plugin has an editor)" : "parameter-0 toggle quirk");
    if (e->initialDelay != p.initialDelay)
        rep.info("initialDelay changed during prepare: %d -> %d samples", p.initialDelay, (int)e->initialDelay);
    if (e->initialDelay > 0) rep.info("latency: %d samples", (int)e->initialDelay);

    // Snapshot the freshly loaded state so later checks can start from it.
    std::vector<float> defaultVals((size_t)numParams);
    for (int i = 0; i < numParams; ++i) defaultVals[(size_t)i] = getParam(e, i);
    std::vector<uint8_t> defaultChunk;
    if (p.hasChunks)
    {
        void* ptr = nullptr;
        VstIntPtr n = disp(e, effGetChunk, 0, 0, &ptr);
        if (n > 0 && ptr) defaultChunk.assign((const uint8_t*)ptr, (const uint8_t*)ptr + n);
    }
    auto restoreDefaults = [&]()
    {
        setPhase("restoring the default state");
        for (int i = 0; i < numParams; ++i) setParam(e, i, defaultVals[(size_t)i]);
        if (!defaultChunk.empty()) disp(e, effSetChunk, 0, (VstIntPtr)defaultChunk.size(), defaultChunk.data());
    };

    std::mt19937 rng(opt.seed);
    std::uniform_real_distribution<float> uni(0.0f, 1.0f);
    Renderer r(p, rep);
    std::vector<int16_t> wavData;
    if (!opt.wav.empty()) r.wav = &wavData;

    // ---- main render --------------------------------------------------------
    rep.section("Render");
    const bool instrument = p.isSynth;
    std::vector<MidiEv> mainEvents;
    if (instrument) mainEvents = noteEvents(opt.notes, 0.1, opt.seconds * 0.6);
    PassResult main = r.run("render (main pass)", opt.seconds, opt.block, mainEvents, !instrument, false, true, nullptr);
    rep.info("main pass: %ld blocks, peak %.2f dBFS, rms %.2f dBFS", main.blocks, toDb(main.all.peak), toDb(main.all.rms()));
    checkAudio(rep, "main pass", main.all);
    if (instrument)
    {
        if (main.all.peak <= 0.0) rep.warn("instrument produced silence: no output despite note-on events (check your MIDI handling)");
        else if (main.all.peak < 1e-4) rep.warn("instrument output is extremely quiet (peak %.1f dBFS)", toDb(main.all.peak));
        else rep.pass("instrument produced sound in response to MIDI notes");
    }
    else
    {
        if (main.all.peak <= 0.0) rep.warn("effect output is completely silent for a -12 dBFS input (bypassed? gain at zero?)");
        else rep.pass("effect produced output from the test signal");
    }
    if (!main.all.nans && !main.all.infs) rep.pass("main pass: no NaN/Inf in the output");

    // ---- (1) block size passes ------------------------------------------------
    rep.section("Check 1: block sizes 32 and 512 (re-prepare like the MPC)");
    const int sizes[2] = {32, 512};
    for (int bs : sizes)
    {
        mpcReprepare(p, bs);
        std::vector<MidiEv> ev;
        if (instrument) ev = noteEvents(opt.notes, 0.05, 0.3);
        char phase[64]; std::snprintf(phase, sizeof(phase), "render (block size %d)", bs);
        PassResult pr = r.run(phase, 0.5, bs, ev, !instrument, false, false, nullptr);
        char what[64]; std::snprintf(what, sizeof(what), "block %d", bs);
        checkAudio(rep, what, pr.all);
        if (!pr.all.nans && !pr.all.infs)
            rep.pass("block size %d (told %d): %ld blocks ok, peak %.2f dBFS", bs, std::max(128, bs), pr.blocks, toDb(pr.all.peak));
    }
    mpcReprepare(p, opt.block);

    // ---- (2) random parameter sweep -----------------------------------------
    rep.section("Check 2: random parameter sweep (200 setParameter calls between blocks)");
    if (numParams == 0) rep.info("plugin has no parameters; sweep skipped");
    else
    {
        int mismatches = 0, quantised = 0, sets = 0;
        std::vector<float> lastSet((size_t)numParams, -1.0f);
        auto hook = [&](long blockIdx)
        {
            if (blockIdx >= 200) return;
            int idx = (int)(rng() % (unsigned)numParams);
            float v = uni(rng);
            if ((blockIdx % 7) == 0) v = (blockIdx % 14) == 0 ? 0.0f : 1.0f;   // hit the extremes too
            setParam(e, idx, v);
            lastSet[(size_t)idx] = v;
            ++sets;
            float back = getParam(e, idx);
            if (std::fabs(back - v) > 1e-4f)
            {
                // Plugins that quantise discrete parameters return a different number but the same text.
                std::string shown = getStr(e, effGetParamDisplay, idx);
                setParam(e, idx, v);
                std::string again = getStr(e, effGetParamDisplay, idx);
                if (shown == again && std::fabs(getParam(e, idx) - back) <= 1e-4f) ++quantised;
                else
                {
                    ++mismatches;
                    if (mismatches <= 5)
                        rep.fail("param %d \"%s\": set %.5f but getParameter returned %.5f", idx,
                                 p.paramNames[(size_t)idx].c_str(), (double)v, (double)back);
                }
            }
        };
        std::vector<MidiEv> ev;
        if (instrument) ev = noteEvents(opt.notes, 0.0, 0.55);
        double sweepSeconds = std::max(0.6, 205.0 * opt.block / opt.sr);
        PassResult pr = r.run("render (parameter sweep)", sweepSeconds, opt.block, ev, !instrument, false, false, hook);
        checkAudio(rep, "parameter sweep", pr.all, true);
        int finalMismatch = 0;
        for (int i = 0; i < numParams; ++i)
            if (lastSet[(size_t)i] >= 0.0f && std::fabs(getParam(e, i) - lastSet[(size_t)i]) > 1e-4f) ++finalMismatch;
        if (mismatches) rep.fail("parameter sweep: %d of %d setParameter calls did not round-trip through getParameter", mismatches, sets);
        else rep.pass("parameter sweep: %d random setParameter calls round-trip through getParameter%s", sets,
                      quantised ? " (some values quantised by the plugin, text unchanged - fine)" : "");
        if (!mismatches && finalMismatch) rep.info("%d parameter(s) hold a quantised value after the sweep", finalMismatch);
        if (!pr.all.nans && !pr.all.infs) rep.pass("parameter sweep: audio stayed finite while parameters changed");
    }

    // ---- (3) state round trip -------------------------------------------------
    rep.section("Check 3: state round-trip (effGetChunk -> randomise -> effSetChunk)");
    if (!p.hasChunks) rep.warn("plugin does not set effFlagsProgramChunks; the MPC saves plugin state via chunks, so settings will not be saved in projects");
    else
    {
        setPhase("state round-trip");
        void* chunkPtr = nullptr;
        VstIntPtr n = disp(e, effGetChunk, 0, 0, &chunkPtr);
        if (n <= 0 || chunkPtr == nullptr) rep.fail("effGetChunk(0) returned %ld bytes (pointer %s)", (long)n, chunkPtr ? "set" : "NULL");
        else
        {
            std::vector<uint8_t> saved((const uint8_t*)chunkPtr, (const uint8_t*)chunkPtr + n);
            std::vector<float> savedVals((size_t)numParams);
            std::vector<std::string> savedText((size_t)numParams);
            for (int i = 0; i < numParams; ++i) { savedVals[(size_t)i] = getParam(e, i); savedText[(size_t)i] = getStr(e, effGetParamDisplay, i); }
            rep.info("state chunk is %ld bytes", (long)n);

            for (int i = 0; i < numParams; ++i) setParam(e, i, uni(rng));
            std::vector<MidiEv> none;
            r.run("render (after randomising parameters)", 2.0 * opt.block / opt.sr, opt.block, none, !instrument, false, false, nullptr);

            setPhase("effSetChunk");
            VstIntPtr ok = disp(e, effSetChunk, 0, n, saved.data());
            r.run("render (after effSetChunk)", 2.0 * opt.block / opt.sr, opt.block, none, !instrument, false, false, nullptr);
            int bad = 0, quant = 0;
            for (int i = 0; i < numParams; ++i)
            {
                float now = getParam(e, i);
                if (std::fabs(now - savedVals[(size_t)i]) <= 1e-4f) continue;
                if (getStr(e, effGetParamDisplay, i) == savedText[(size_t)i]) { ++quant; continue; }
                ++bad;
                if (bad <= 5)
                    rep.fail("param %d \"%s\": saved %.5f but after effSetChunk it is %.5f (\"%s\")", i,
                             p.paramNames[(size_t)i].c_str(), (double)savedVals[(size_t)i], (double)now,
                             getStr(e, effGetParamDisplay, i).c_str());
            }
            if (ok == 0) rep.info("effSetChunk returned 0 (the MPC ignores the return value)");
            if (bad) rep.fail("state round-trip: %d parameter(s) did not come back from the saved chunk", bad);
            else rep.pass("state round-trip: all %d parameters restored from the chunk%s", numParams,
                          quant ? " (discrete values quantised, text identical)" : "");

            void* again = nullptr;
            VstIntPtr n2 = disp(e, effGetChunk, 0, 0, &again);
            if (n2 == n && again && std::memcmp(again, saved.data(), (size_t)n) == 0)
                rep.pass("state round-trip: re-saved chunk is byte-identical");
            else
                rep.warn("state round-trip: re-saved chunk differs from the original (%ld vs %ld bytes) - fine if your state has non-parameter content", (long)n2, (long)n);

            // Robustness: garbage must not crash or corrupt.
            uint8_t junk[64];
            for (uint8_t& b : junk) b = (uint8_t)(rng() & 0xff);
            setPhase("effSetChunk with invalid data");
            VstIntPtr jr = disp(e, effSetChunk, 0, (VstIntPtr)sizeof(junk), junk);
            disp(e, effSetChunk, 0, 0, nullptr);
            rep.pass("effSetChunk tolerated invalid data without crashing (returned %ld)", (long)jr);
            disp(e, effSetChunk, 0, n, saved.data());
        }
    }

    // ---- (7) in-place processing (JUCE hosts pass the same buffers) ----------
    restoreDefaults();
    if (!instrument && e->numInputs > 0)
    {
        rep.section("Check: in-place processing (input and output share memory)");
        std::vector<MidiEv> none;
        double len = 8.0 * opt.block / opt.sr;
        mpcReprepare(p, opt.block);
        PassResult oop = r.run("render (out-of-place reference)", len, opt.block, none, true, false, false, nullptr);
        mpcReprepare(p, opt.block);
        PassResult ip = r.run("render (in-place)", len, opt.block, none, true, true, false, nullptr);
        checkAudio(rep, "in-place", ip.all);
        double a = oop.all.rms(), b = ip.all.rms();
        if (a > 1e-6 && b < 0.1 * a)
            rep.fail("in-place: output almost vanishes (rms %.1f dBFS vs %.1f dBFS) when the host passes the SAME buffers for inputs and outputs. "
                     "The MPC does exactly that: never clear or write an output channel before you have read the input sample at the same position.",
                     toDb(b), toDb(a));
        else if (a > 1e-6 && std::fabs(toDb(a) - toDb(b)) > 1.0)
            rep.info("in-place output level differs from out-of-place by %.1f dB (fine if your plugin has randomness or carries state)", toDb(b) - toDb(a));
        else
            rep.pass("in-place processing matches out-of-place (rms %.1f dBFS)", toDb(b));
    }

    // ---- (4) all notes off + silence ----------------------------------------
    rep.section("Check 4: all-notes-off and 0.5 s of silence");
    {
        restoreDefaults();
        mpcReprepare(p, opt.block);
        std::vector<MidiEv> ev;
        if (instrument)
        {
            for (int n : opt.notes) ev.push_back({0, 0x80, (uint8_t)n, 0});
            ev.push_back({0, 0xB0, 123, 0});   // all notes off
            ev.push_back({0, 0xB0, 120, 0});   // all sound off
        }
        PassResult pr = r.run("render (silence after all-notes-off)", 0.5, opt.block, ev, false, false, false, nullptr);
        checkAudio(rep, "silence pass", pr.all);
        double tailPeak = pr.tail.peak;
        if (instrument)
        {
            if (tailPeak > 0.01) rep.warn("instrument is still sounding %.1f dBFS 0.5 s after all-notes-off (long release? stuck voice?)", toDb(tailPeak));
            else if (tailPeak > 1e-5) rep.info("instrument decays to %.1f dBFS after 0.5 s (release tail)", toDb(tailPeak));
            else rep.pass("instrument went quiet after all-notes-off (peak %.1f dBFS in the last 100 ms)", toDb(tailPeak));
        }
        else
        {
            if (tailPeak > 1e-4) rep.warn("effect outputs %.1f dBFS with silent input (self-oscillation, noise or DC offset?)", toDb(tailPeak));
            else rep.pass("effect passes silence (peak %.1f dBFS in the last 100 ms)", toDb(tailPeak));
        }
    }

    // ---- (5) summary of output checks ---------------------------------------
    rep.section("Check 5: output sanity (all passes)");
    if (main.all.nans == 0 && main.all.infs == 0) rep.pass("no NaN/Inf anywhere in the main pass");
    if (main.all.clips == 0) rep.pass("no clipping in the main pass (peak %.2f dBFS)", toDb(main.all.peak));
    if (main.all.denormals == 0) rep.pass("no denormals in the main pass");

    // ---- (6) timing -----------------------------------------------------------
    rep.section("Check 6: timing");
    double blockMs = 1000.0 * opt.block / opt.sr;
    rep.info("block of %d frames = %.2f ms; processReplacing took on average %.2f%% of that, worst block %.2f%% (desktop CPU, for orientation only)",
             opt.block, blockMs, main.cpuAvg, main.cpuMax);
    if (main.cpuAvg > 50.0) rep.warn("average CPU use is above 50%% of real time on this machine; the MPC's ARM CPU is much slower");
    else rep.pass("timing: average %.2f%% CPU per block", main.cpuAvg);

    // ---- host callbacks summary --------------------------------------------
    rep.section("Host callbacks seen");
    rep.info("audioMasterGetTime %ld, Automate %ld, UpdateDisplay %ld, IOChanged %ld, ProcessEvents %ld calls (%ld MIDI events out)%s",
             g.getTimeCalls, g.automateCalls, g.updateDisplayCalls, g.ioChangedCalls, g.midiOutCalls, g.midiOutEvents,
             g.midiOutEvents ? " - note: the MPC ignores plugin MIDI output" : "");
    if (g.unknownCalls)
    {
        std::string ops;
        for (int o : g.unknownOpcodes) ops += (ops.empty() ? "" : ", ") + masterName(o);
        rep.info("%ld call(s) to host opcodes the MPC does not implement (answered 0): %s", g.unknownCalls, ops.c_str());
    }

    // ---- teardown --------------------------------------------------------------
    rep.section("Teardown");
    mpcClose(p);
    rep.pass("effStopProcess, effMainsChanged(0), effClose");

    if (!opt.wav.empty())
    {
        if (writeWav(opt.wav, wavData, (int)opt.sr, 2)) rep.info("wrote %s (%.2f s, 16-bit stereo, main pass only)", opt.wav.c_str(), opt.seconds);
        else rep.warn("could not write %s", opt.wav.c_str());
    }

    std::printf("\nResult: %s  (%d passed, %d warnings, %d failures)\n",
                rep.fails ? "FAIL" : (rep.warns ? "PASS with warnings" : "PASS"), rep.passes, rep.warns, rep.fails);
    return rep.fails ? 1 : 0;
}

// ===========================================================================
// main
// ===========================================================================

static void usage()
{
    std::fprintf(stderr,
        "host_sim - a tiny host that behaves like the Akai MPC's VST2 host\n\n"
        "  host_sim <plugin.dylib|.so|.dll> dump\n"
        "      print params.json (name, vendor, uid, programs, parameters with inferred kinds)\n\n"
        "  host_sim <plugin> test [options]\n"
        "      --seconds N     length of the main render (default 2)\n"
        "      --sr N          sample rate (default 44100, the MPC's rate)\n"
        "      --block N       frames per process call (default 128)\n"
        "      --wav out.wav   write the main pass as 16-bit stereo WAV\n"
        "      --notes a,b,c   MIDI notes to play for instruments (default 60)\n"
        "      --playing       report the transport as playing\n"
        "      --seed N        seed for the random parameter tests (default 1234)\n"
        "      --verbose       log every opcode in both directions\n\n"
        "Exit code: 0 pass, 1 failures (or bad arguments), 2 the plugin crashed.\n");
}

int main(int argc, char** argv)
{
    installCrashHandlers();
    if (argc < 3) { usage(); return 1; }
    std::string path = argv[1];
    std::string cmd = argv[2];

    if (cmd == "dump")
    {
        for (int i = 3; i < argc; ++i) if (!std::strcmp(argv[i], "--verbose")) g.verbose = true;
        return runDump(path);
    }
    if (cmd != "test") { usage(); return 1; }

    TestOptions opt;
    for (int i = 3; i < argc; ++i)
    {
        std::string a = argv[i];
        auto next = [&](const char* name) -> const char* {
            if (i + 1 >= argc) { std::fprintf(stderr, "host_sim: %s needs a value\n", name); std::exit(1); }
            return argv[++i];
        };
        if (a == "--seconds") opt.seconds = std::atof(next("--seconds"));
        else if (a == "--sr") opt.sr = std::atof(next("--sr"));
        else if (a == "--block") opt.block = std::atoi(next("--block"));
        else if (a == "--wav") opt.wav = next("--wav");
        else if (a == "--seed") opt.seed = (unsigned)std::strtoul(next("--seed"), nullptr, 10);
        else if (a == "--notes")
        {
            opt.notes.clear();
            std::string s = next("--notes");
            size_t pos = 0;
            while (pos <= s.size())
            {
                size_t comma = s.find(',', pos);
                std::string tok = s.substr(pos, comma == std::string::npos ? std::string::npos : comma - pos);
                if (!tok.empty()) opt.notes.push_back(std::max(0, std::min(127, std::atoi(tok.c_str()))));
                if (comma == std::string::npos) break;
                pos = comma + 1;
            }
            if (opt.notes.empty()) opt.notes.push_back(60);
        }
        else if (a == "--verbose") opt.verbose = true;
        else if (a == "--playing") opt.playing = true;
        else { std::fprintf(stderr, "host_sim: unknown option %s\n", a.c_str()); usage(); return 1; }
    }
    if (opt.seconds <= 0.05 || opt.sr < 8000 || opt.block < 1 || opt.block > 4096)
    {
        std::fprintf(stderr, "host_sim: --seconds must be > 0.05, --sr >= 8000, --block 1..4096\n");
        return 1;
    }
    return runTest(path, opt);
}

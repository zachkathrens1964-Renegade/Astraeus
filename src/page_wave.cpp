// the wave link page: one mix's faders from elgato wave link 3, over its local socket
// the socket is the one elgato's own stream deck plugin talks to: unofficial, so a wave link update can change it
#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstdio>
#include <cctype>
#include <cstring>
#include <cwctype>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include <windows.h>
#include <winhttp.h>

#include "glyphs.h"
#include "page_sheet.h"
#include "pages.h"

#pragma comment(lib, "winhttp.lib")

namespace {

constexpr wchar_t kMixVal[] = L"WaveLinkMix"; // the mix picked last, by name
constexpr int kPortLo = 1884, kPortHi = 1893; // where wave link 3 listens when its port file can't be read
constexpr DWORD kRetryMs = 3000, kIdleMs = 60000; // between tries while it's closed; the link lets go this long after the page was last shown
constexpr size_t kMaxJson = 4 << 20;
constexpr int kMaxDepth = 32;
constexpr double kHoldSec = 0.6; // after a fader is let go, its own value wins over echoes still on the way
constexpr double kSendEvery = 0.03; // seconds between level sends while dragging
constexpr float kStripW = 104, kStripGap = 10, kHeadH = 44, kTrackW = 8, kKnobR = 9, kFootH = 40, kWheelStep = 0.05f;

// ---- a small json reader, as the workspaces source has ----
struct J {
    enum Type { kNull, kBool, kNum, kStr, kArr, kObj } type = kNull;
    bool b = false;
    double n = 0;
    std::string s;
    std::vector<J> a;
    std::vector<std::pair<std::string, J>> o;
    const J& operator[](const char* key) const {
        static const J none;
        if (type == kObj)
            for (const auto& kv : o)
                if (kv.first == key) return kv.second;
        return none;
    }
    bool Has(const char* key) const {
        if (type == kObj)
            for (const auto& kv : o)
                if (kv.first == key) return true;
        return false;
    }
};

class Reader {
public:
    Reader(const char* begin, const char* end) : p(begin), e(end) {}
    bool Parse(J& out, int depth = 0) {
        if (depth > kMaxDepth) return false;
        Skip();
        if (p >= e) return false;
        char c = *p;
        if (c == '{') return Obj(out, depth);
        if (c == '[') return Arr(out, depth);
        if (c == '"') return out.type = J::kStr, Str(out.s);
        if (c == 't' || c == 'f') return Word(out, c == 't');
        if (c == 'n') {
            if (e - p < 4 || strncmp(p, "null", 4)) return false;
            p += 4, out.type = J::kNull;
            return true;
        }
        return Num(out);
    }

private:
    const char *p, *e;
    void Skip() {
        while (p < e && (*p == ' ' || *p == '\n' || *p == '\r' || *p == '\t')) ++p;
    }
    bool Word(J& out, bool v) {
        size_t len = v ? 4 : 5;
        if ((size_t)(e - p) < len || strncmp(p, v ? "true" : "false", len)) return false;
        p += len, out.type = J::kBool, out.b = v;
        return true;
    }
    bool Num(J& out) {
        std::string tmp(p, (std::min)((size_t)(e - p), (size_t)64));
        char* end = nullptr;
        out.n = strtod(tmp.c_str(), &end);
        if (end == tmp.c_str()) return false;
        p += end - tmp.c_str(), out.type = J::kNum;
        return true;
    }
    static void Utf8(std::string& s, unsigned cp) {
        if (cp < 0x80) s += (char)cp;
        else if (cp < 0x800) s += (char)(0xC0 | (cp >> 6)), s += (char)(0x80 | (cp & 0x3F));
        else s += (char)(0xE0 | (cp >> 12)), s += (char)(0x80 | ((cp >> 6) & 0x3F)), s += (char)(0x80 | (cp & 0x3F));
    }
    bool Str(std::string& s) {
        ++p;
        while (p < e && *p != '"') {
            if (*p != '\\') {
                s += *p++;
                continue;
            }
            if (++p >= e) return false;
            char c = *p++;
            switch (c) {
            case 'n': s += '\n'; break;
            case 't': s += '\t'; break;
            case 'r': s += '\r'; break;
            case 'b': s += '\b'; break;
            case 'f': s += '\f'; break;
            case 'u':
                if (e - p < 4) return false;
                Utf8(s, (unsigned)strtoul(std::string(p, 4).c_str(), nullptr, 16));
                p += 4;
                break;
            default: s += c; break;
            }
        }
        if (p >= e) return false;
        ++p;
        return true;
    }
    bool Arr(J& out, int depth) {
        out.type = J::kArr;
        ++p;
        Skip();
        if (p < e && *p == ']') return ++p, true;
        while (p < e) {
            out.a.emplace_back();
            if (!Parse(out.a.back(), depth + 1)) return false;
            Skip();
            if (p < e && *p == ',') {
                ++p;
                continue;
            }
            if (p < e && *p == ']') return ++p, true;
            return false;
        }
        return false;
    }
    bool Obj(J& out, int depth) {
        out.type = J::kObj;
        ++p;
        Skip();
        if (p < e && *p == '}') return ++p, true;
        while (p < e) {
            Skip();
            std::string key;
            if (p >= e || *p != '"' || !Str(key)) return false;
            Skip();
            if (p >= e || *p != ':') return false;
            ++p;
            out.o.emplace_back(std::move(key), J{});
            if (!Parse(out.o.back().second, depth + 1)) return false;
            Skip();
            if (p < e && *p == ',') {
                ++p;
                continue;
            }
            if (p < e && *p == '}') return ++p, true;
            return false;
        }
        return false;
    }
};

bool ParseJson(const std::string& text, J& out) {
    Reader r(text.data(), text.data() + text.size());
    return r.Parse(out);
}

std::wstring Wide(const std::string& s) {
    if (s.empty()) return {};
    int n = MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(), nullptr, 0);
    std::wstring w(n, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(), w.data(), n);
    return w;
}

std::string Quote(const std::string& s) { // a json string
    std::string out = "\"";
    for (char ch : s) {
        if (ch == '"' || ch == '\\') out += '\\', out += ch;
        else if ((unsigned char)ch < 0x20) {
            char buf[8];
            snprintf(buf, sizeof(buf), "\\u%04x", (unsigned)(unsigned char)ch);
            out += buf;
        } else out += ch;
    }
    return out + "\"";
}

std::string Num(float v) {
    char buf[32];
    snprintf(buf, sizeof(buf), "%.4f", (double)(std::clamp)(v, 0.0f, 1.0f));
    return buf;
}

// ---- what wave link holds ----
struct Send {
    std::string mix; // a mix's id
    float level = 1;
    bool muted = false;
};
struct Channel {
    std::string id, type;
    std::wstring name;
    float level = 1;
    bool muted = false;
    std::vector<Send> sends; // its level in each mix
};
struct Mix {
    std::string id;
    std::wstring name;
    float level = 1;
    bool muted = false;
};

struct Link { // the worker's side and the page's, under one lock
    std::mutex lock;
    std::vector<Channel> channels;
    std::vector<Mix> mixes;
    int state = 0; // 0 looking, 1 connected, 2 not wave link 3
    uint64_t version = 0; // bumped on every change the page should see
    std::atomic<bool> running{false}, stop{false};
    std::atomic<ULONGLONG> seen{0}; // when the page last drew
    HINTERNET socket = nullptr;
    std::mutex sendMu;
    std::atomic<int> nextId{1};

    bool Post(const std::string& method, const std::string& params) {
        std::string msg = "{\"jsonrpc\":\"2.0\",\"method\":" + Quote(method) + ",\"params\":" + params + ",\"id\":" + std::to_string(nextId++) + "}";
        std::lock_guard<std::mutex> g(sendMu);
        return socket && WinHttpWebSocketSend(socket, WINHTTP_WEB_SOCKET_UTF8_MESSAGE_BUFFER_TYPE, (PVOID)msg.data(), (DWORD)msg.size()) == NO_ERROR;
    }
};
std::shared_ptr<Link> link = std::make_shared<Link>();

Channel ReadChannel(const J& c) {
    Channel ch;
    ch.id = c["id"].s, ch.type = c["type"].s, ch.name = Wide(c["name"].s);
    ch.level = (float)c["level"].n, ch.muted = c["isMuted"].b;
    for (const J& m : c["mixes"].a) ch.sends.push_back({m["id"].s, (float)m["level"].n, m["isMuted"].b});
    return ch;
}

Mix ReadMix(const J& m) { return {m["id"].s, Wide(m["name"].s), (float)m["level"].n, m["isMuted"].b}; }

void Patch(Channel& ch, const J& p) { // a channelChanged: only what changed is in it, the mixes whole
    if (p.Has("name")) ch.name = Wide(p["name"].s);
    if (p.Has("level")) ch.level = (float)p["level"].n;
    if (p.Has("isMuted")) ch.muted = p["isMuted"].b;
    if (p["mixes"].type == J::kArr) {
        std::vector<Send> next;
        for (const J& m : p["mixes"].a) {
            Send s{m["id"].s};
            auto old = std::find_if(ch.sends.begin(), ch.sends.end(), [&](const Send& x) { return x.mix == s.mix; });
            if (old != ch.sends.end()) s = *old;
            if (m.Has("level")) s.level = (float)m["level"].n;
            if (m.Has("isMuted")) s.muted = m["isMuted"].b;
            next.push_back(s);
        }
        ch.sends = std::move(next);
    }
}

void Take(Link& l, const J& doc) {
    std::lock_guard<std::mutex> g(l.lock);
    const J& r = doc["result"];
    const J& prm = doc["params"];
    const std::string& method = doc["method"].s;
    if (r["channels"].type == J::kArr || (method == "channelsChanged" && prm["channels"].type == J::kArr)) {
        const J& list = r["channels"].type == J::kArr ? r["channels"] : prm["channels"];
        l.channels.clear();
        for (const J& c : list.a) l.channels.push_back(ReadChannel(c));
    } else if (r["mixes"].type == J::kArr || (method == "mixesChanged" && prm["mixes"].type == J::kArr)) {
        const J& list = r["mixes"].type == J::kArr ? r["mixes"] : prm["mixes"];
        l.mixes.clear();
        for (const J& m : list.a) l.mixes.push_back(ReadMix(m));
    } else if (method == "channelChanged") {
        for (Channel& ch : l.channels)
            if (ch.id == prm["id"].s) Patch(ch, prm);
    } else if (method == "mixChanged") {
        for (Mix& m : l.mixes)
            if (m.id == prm["id"].s) {
                if (prm.Has("name")) m.name = Wide(prm["name"].s);
                if (prm.Has("level")) m.level = (float)prm["level"].n;
                if (prm.Has("isMuted")) m.muted = prm["isMuted"].b;
            }
    } else if (method == "channelsChanged" || method == "mixesChanged") { // told only that something changed: ask for the list
        l.Post(method == "channelsChanged" ? "getChannels" : "getMixes", "{}");
        return;
    } else return;
    ++l.version;
}

int PortFromFile() { // wave link writes where it listens
    wchar_t base[MAX_PATH] = {};
    if (!GetEnvironmentVariableW(L"LOCALAPPDATA", base, MAX_PATH)) return 0;
    std::wstring path = std::wstring(base) + L"\\Packages\\Elgato.WaveLink_g54w8ztgkx496\\LocalState\\ws-info.json";
    HANDLE f = CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr, OPEN_EXISTING, 0, nullptr);
    if (f == INVALID_HANDLE_VALUE) return 0;
    char buf[4096];
    DWORD got = 0;
    BOOL ok = ReadFile(f, buf, sizeof(buf) - 1, &got, nullptr);
    CloseHandle(f);
    if (!ok) return 0;
    J doc;
    if (!ParseJson(std::string(buf, got), doc)) return 0;
    int port = (int)doc["port"].n;
    return port > 0 && port < 65536 ? port : 0;
}

bool Session(Link& l, int port) { // one connection, held until wave link goes or the page has been away a while; false if nothing answered as wave link
    HINTERNET session = WinHttpOpen(L"Astraeus", WINHTTP_ACCESS_TYPE_NO_PROXY, WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0);
    HINTERNET conn = session ? WinHttpConnect(session, L"127.0.0.1", (INTERNET_PORT)port, 0) : nullptr;
    HINTERNET req = conn ? WinHttpOpenRequest(conn, L"GET", L"/", nullptr, WINHTTP_NO_REFERER, WINHTTP_DEFAULT_ACCEPT_TYPES, 0) : nullptr;
    HINTERNET ws = nullptr;
    if (session) WinHttpSetTimeouts(session, 1000, 1000, 2000, 3000); // a port that isn't wave link answers fast or not at all
    const wchar_t kOrigin[] = L"Origin: streamdeck://"; // what wave link lets in
    if (req && WinHttpSetOption(req, WINHTTP_OPTION_UPGRADE_TO_WEB_SOCKET, nullptr, 0) &&
        WinHttpSendRequest(req, kOrigin, (DWORD)-1L, nullptr, 0, 0, 0) && WinHttpReceiveResponse(req, nullptr))
        ws = WinHttpWebSocketCompleteUpgrade(req, 0);
    if (req) WinHttpCloseHandle(req);
    bool ours = false;
    if (ws) {
        DWORD never = 0; // a receive waits as long as the link lives: stop closes the socket to wake it
        WinHttpSetOption(ws, WINHTTP_OPTION_RECEIVE_TIMEOUT, &never, sizeof(never));
        {
            std::lock_guard<std::mutex> g(l.sendMu);
            l.socket = ws;
        }
        l.Post("getApplicationInfo", "{}");
        std::string text;
        std::vector<char> chunk(1 << 16);
        std::thread watch([&l, ws] { // lets go once the page has been away a while
            while (!l.stop.load()) {
                Sleep(500);
                if (GetTickCount64() - l.seen.load() > kIdleMs) {
                    l.stop = true;
                    break;
                }
            }
            std::lock_guard<std::mutex> g(l.sendMu);
            if (l.socket == ws) WinHttpWebSocketClose(ws, WINHTTP_WEB_SOCKET_SUCCESS_CLOSE_STATUS, nullptr, 0); // wakes the receive below
        });
        while (!l.stop.load()) {
            DWORD got = 0;
            WINHTTP_WEB_SOCKET_BUFFER_TYPE type;
            DWORD err = WinHttpWebSocketReceive(ws, chunk.data(), (DWORD)chunk.size(), &got, &type);
            if (err == ERROR_WINHTTP_TIMEOUT) continue; // quiet, not gone
            if (err != NO_ERROR || type == WINHTTP_WEB_SOCKET_CLOSE_BUFFER_TYPE) break;
            text.append(chunk.data(), got);
            if (text.size() > kMaxJson) text.clear();
            if (type == WINHTTP_WEB_SOCKET_UTF8_FRAGMENT_BUFFER_TYPE || type == WINHTTP_WEB_SOCKET_BINARY_FRAGMENT_BUFFER_TYPE) continue;
            J doc;
            if (ParseJson(text, doc)) {
                const J& r = doc["result"];
                if (r.Has("appID")) { // the first answer: is this wave link 3
                    if (r["appID"].s != "EWL") {
                        text.clear();
                        break;
                    }
                    ours = true;
                    {
                        std::lock_guard<std::mutex> g(l.lock);
                        l.state = 1;
                        ++l.version;
                    }
                    l.Post("getMixes", "{}");
                    l.Post("getChannels", "{}");
                } else if (ours) Take(l, doc);
            }
            text.clear();
        }
        bool stopped = l.stop.load();
        l.stop = true; // the watcher ends
        watch.join();
        l.stop = stopped;
        {
            std::lock_guard<std::mutex> g(l.sendMu);
            l.socket = nullptr;
        }
        WinHttpCloseHandle(ws);
    }
    if (conn) WinHttpCloseHandle(conn);
    if (session) WinHttpCloseHandle(session);
    std::lock_guard<std::mutex> g(l.lock);
    if (l.state == 1) l.state = 0, ++l.version;
    return ours;
}

void Start() {
    std::shared_ptr<Link> l = link;
    l->seen = GetTickCount64();
    if (l->running.exchange(true)) return;
    l->stop = false;
    try {
        std::thread([l] {
            SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_BELOW_NORMAL);
            int scan = kPortLo;
            while (!l->stop.load() && GetTickCount64() - l->seen.load() < kIdleMs) {
                int port = PortFromFile();
                bool found = Session(*l, port ? port : scan);
                if (!found && !port) { // no port file: walk the range, a quick try each
                    scan = scan >= kPortHi ? kPortLo : scan + 1;
                    if (scan != kPortLo) continue;
                }
                for (DWORD waited = 0; waited < kRetryMs && !l->stop.load(); waited += 250) Sleep(250);
            }
            {
                std::lock_guard<std::mutex> g(l->lock);
                l->channels.clear(), l->mixes.clear(), l->state = 0, ++l->version;
            }
            l->running = false;
        }).detach();
    } catch (...) {
        l->running = false;
    }
}

// ---- the page ----
struct Held { // a fader the page moved: its own value shows while it moves and a moment after, so wave link's echoes can't pull it back
    std::string key;
    float level = 0;
    double released = 0, sentAt = -1; // released 0: still held
    bool dirty = false; // moved since the last send
};
std::vector<Held> held;
std::vector<Channel> channels;
std::vector<Mix> mixes;
uint64_t seenVersion = ~0ull;
int state = 0;
std::wstring pickedMix;
bool pickLoaded = false;
float scrollTo = 0;

Held* Hold(const std::string& key) {
    for (Held& h : held)
        if (h.key == key) return &h;
    return nullptr;
}

std::wstring Fit(Canvas& c, const std::wstring& s, Weight w, float size, float room) {
    if (c.Measure(s, w, size) <= room) return s;
    std::wstring cut = s;
    while (!cut.empty() && c.Measure(cut + L"…", w, size) > room) cut.pop_back();
    return cut + L"…";
}

int GlyphFor(const Channel& ch) {
    std::string t = ch.type;
    for (char& x : t) x = (char)tolower((unsigned char)x);
    std::wstring n = ch.name;
    for (wchar_t& x : n) x = (wchar_t)towlower(x);
    if (t.find("mic") != std::string::npos || t.find("input") != std::string::npos || n.find(L"mic") != std::wstring::npos) return glyph::Mic;
    if (t.find("music") != std::string::npos || n.find(L"music") != std::wstring::npos || n.find(L"spotify") != std::wstring::npos) return glyph::Music;
    if (t.find("game") != std::string::npos || n.find(L"game") != std::wstring::npos) return glyph::Pad;
    if (t.find("voice") != std::string::npos || t.find("chat") != std::string::npos || n.find(L"chat") != std::wstring::npos || n.find(L"discord") != std::wstring::npos) return glyph::Chat;
    if (n.find(L"browser") != std::wstring::npos || n.find(L"chrome") != std::wstring::npos) return glyph::Web;
    return glyph::Speaker;
}

// one fader: icon, name, track, level, mute. true while it moves; level and mute are what the page should send
bool Strip(PageCtx& p, const D2D1_RECT_F& r, const D2D1_RECT_F& view, uint32_t id, const std::wstring& name, int icon, bool master, float& level, bool muted, bool& muteTap) {
    Ui& ui = p.ui;
    Canvas& c = ui.C();
    const Theme& t = p.shell;
    bool live = Inside(view, ui.In().mouse) || ui.IsActive(id) || ui.Keyed();
    float cx = (r.left + r.right) / 2;
    float hover = ui.Smooth(id + 20, live && Inside(r, ui.In().mouse) ? 1.0f : 0, 16);
    c.Fill(r, 18, (master ? t.accent.Alpha(0.14f).Mix(t.cardHi.Alpha(0.35f), 0.4f) : t.cardHi.Alpha(0.35f)).Mix(t.cardHi, 0.3f * hover));
    c.StrokeV(r, 18, master ? t.accent.Alpha(0.45f) : t.hairTop, t.hairBot, 1);
    D2D1_POINT_2F ic = {cx, r.top + 24};
    c.Dot(ic, 15, master ? t.accent.Alpha(0.24f) : t.track);
    c.Icon(icon, ic, 16, master ? t.accentHi : t.textDim, master);
    c.Text(Fit(c, name, Weight::Medium, 11.5f, Width(r) - 12), Rect(r.left + 6, r.top + 44, Width(r) - 12, 16), Weight::Medium, 11.5f, t.text, Align::Center);

    float top = r.top + 72, bottom = r.bottom - kFootH - 6;
    D2D1_RECT_F track = {cx - kTrackW / 2, top, cx + kTrackW / 2, (std::max)(top + 20, bottom)};
    D2D1_RECT_F grab = {cx - 24, track.top - kKnobR, cx + 24, track.bottom + kKnobR};
    bool moved = live && ui.Slider(id, {grab.left, track.top + kKnobR, grab.right, track.bottom - kKnobR}, level, true);
    if (live) ui.Name(name, w::Pct(level));
    if (live && Inside(grab, ui.In().mouse)) {
        float n = ui.Wheel(grab);
        if (n != 0) level = (std::clamp)(level + n * kWheelStep, 0.0f, 1.0f), moved = true;
    }
    float shown = ui.Smooth(id + 21, level, moved ? 40.0f : 14.0f);
    float mute = ui.Smooth(id + 22, muted ? 1.0f : 0, 14);
    float hot = ui.Smooth(id + 26, live && (Inside(grab, ui.In().mouse) || ui.IsActive(id)) ? 1.0f : 0, 16);
    Color fill = t.accent.Mix(t.textLow, mute);
    c.Fill(track, kTrackW / 2, t.track);
    float y = track.bottom - w::ThumbAt(0, Height(track), kKnobR, shown), end = track.bottom - Height(track) * shown;
    c.Fill({track.left, (std::min)(end, track.bottom - kTrackW), track.right, track.bottom}, kTrackW / 2, fill);
    if (hot > 0.01f) c.Dot({cx, y}, kKnobR + 5, fill.Alpha(0.18f * hot));
    c.Dot({cx, y}, kKnobR + hot, t.text);
    c.Dot({cx, y}, 3, fill);

    float fy = r.bottom - kFootH / 2 - 2;
    c.Text(muted ? std::wstring(L"Muted") : w::Pct(level), Rect(r.left + 8, fy - 9, Width(r) / 2 + 4, 18), Weight::Semi, 12, muted ? t.textLow : t.text, Align::Center);
    D2D1_POINT_2F mb = {r.right - 26, fy};
    Ui::Hit h = live ? ui.Button(id + 23, {mb.x - 14, mb.y - 14, mb.x + 14, mb.y + 14}) : Ui::Hit{};
    if (live) ui.Name(muted ? L"Unmute " + name : L"Mute " + name);
    float mh = ui.Smooth(id + 24, h.hover ? 1.0f : 0, 16);
    c.Dot(mb, 13, (muted ? fill.Alpha(0.30f) : t.track).Mix(t.text.Alpha(0.15f), 0.5f * mh));
    bool mic = icon == glyph::Mic;
    c.Icon(muted ? (mic ? glyph::MicOff : glyph::VolumeOff) : (mic ? glyph::Mic : glyph::VolumeUp), mb, 15, muted ? t.accentHi : t.textDim.Mix(t.text, mh), muted);
    muteTap = h.clicked;
    return moved;
}

void Message(PageCtx& p, const D2D1_RECT_F& a, const wchar_t* title, const wchar_t* sub, bool spin) {
    Canvas& c = p.ui.C();
    const Theme& t = p.shell;
    float cx = (a.left + a.right) / 2, cy = (a.top + a.bottom) / 2;
    if (spin) sheet::Spinner(c, {cx, cy - 30}, 9, t.accent, p.ui.Now());
    else c.Icon(glyph::Tune, {cx, cy - 30}, 30, t.textLow);
    c.Text(title, Rect(a.left + 20, cy - 8, Width(a) - 40, 20), Weight::Medium, 14, t.text, Align::Center);
    c.Text(sub, Rect(a.left + 20, cy + 16, Width(a) - 40, 36), Weight::Light, 11.5f, t.textDim, Align::Center);
}

}

void DrawWave(PageCtx& p) {
    Ui& ui = p.ui;
    Canvas& c = ui.C();
    const Theme& t = p.shell;
    const D2D1_RECT_F& a = p.area;
    Start(); // keeps the link up while the page is shown
    if (!pickLoaded) pickLoaded = true, pickedMix = sys::ReadText(kMixVal);
    {
        std::lock_guard<std::mutex> g(link->lock);
        if (link->version != seenVersion) {
            seenVersion = link->version;
            channels = link->channels;
            mixes = link->mixes;
            state = link->state;
        }
    }
    w::Card(ui, t, a);
    if (state != 1 || mixes.empty()) {
        if (state == 1) Message(p, a, L"Loading your mixes…", L"", true);
        else Message(p, a, L"Wave Link isn't connected", L"Open Elgato Wave Link 3 on this PC. This page finds it on its own and shows one mix's sliders.", false);
        ui.KeepAlive(); // looks again soon
        return;
    }

    // which mix: the one picked before, else the personal one, else the first
    int mi = -1;
    for (int i = 0; i < (int)mixes.size() && mi < 0; ++i)
        if (!pickedMix.empty() && mixes[(size_t)i].name == pickedMix) mi = i;
    for (int i = 0; i < (int)mixes.size() && mi < 0; ++i) {
        std::wstring n = mixes[(size_t)i].name;
        for (wchar_t& x : n) x = (wchar_t)towlower(x);
        if (n.find(L"personal") != std::wstring::npos || n.find(L"monitor") != std::wstring::npos) mi = i;
    }
    if (mi < 0) mi = 0;
    Mix& mix = mixes[(size_t)mi];

    // the mixes as chips along the top
    c.TextSp(L"WAVE LINK", Rect(a.left + 18, a.top + 16, 100, 16), Weight::Medium, 10, t.textLow, 1.4f);
    float x = a.left + 110;
    for (int i = 0; i < (int)mixes.size(); ++i) {
        float wdt = c.Measure(mixes[(size_t)i].name, Weight::Medium, 12) + 28;
        if (x + wdt > a.right - 14) break;
        if (w::Chip(ui, t, Ui::Id("wl.mix", sheet::Key(Wide(mixes[(size_t)i].id))), Rect(x, a.top + 10, wdt, 28), mixes[(size_t)i].name, i == mi) && i != mi) {
            pickedMix = mixes[(size_t)i].name;
            sys::SaveText(kMixVal, pickedMix);
            scrollTo = 0;
        }
        x += wdt + 8;
    }

    // the mix's own level first, then every channel's level in it
    D2D1_RECT_F view = {a.left + 12, a.top + kHeadH + 4, a.right - 12, a.bottom - 12};
    int n = 1 + (int)channels.size();
    float content = n * kStripW + (n - 1) * kStripGap;
    float maxS = (std::max)(0.0f, content - Width(view));
    if (!sheet::OverSlider(ui)) scrollTo = (std::clamp)(scrollTo - ui.Wheel(view) * 60, 0.0f, maxS); // a wheel no fader took scrolls across
    float scroll = ui.Smooth(Ui::Id("wl.scroll"), scrollTo, 18);
    double now = ui.Now();
    held.erase(std::remove_if(held.begin(), held.end(), [&](const Held& h) { return h.released > 0 && !h.dirty && now - h.released > kHoldSec; }), held.end());
    c.PushClip(view);
    for (int i = 0; i < n; ++i) {
        D2D1_RECT_F r = Rect(view.left + i * (kStripW + kStripGap) - scroll, view.top, kStripW, Height(view));
        if (r.right < view.left || r.left > view.right) continue;
        bool master = i == 0;
        Channel* ch = master ? nullptr : &channels[(size_t)(i - 1)];
        Send* send = nullptr;
        if (ch)
            for (Send& s : ch->sends)
                if (s.mix == mix.id) send = &s;
        if (ch && !send) continue; // not in this mix
        std::string key = master ? "mix:" + mix.id : ch->id;
        float level = master ? mix.level : send->level;
        bool muted = master ? mix.muted : send->muted;
        if (Held* h = Hold(key)) level = h->level;
        bool tap = false;
        uint32_t id = Ui::Id("wl.strip", sheet::Key(Wide(key)));
        bool moved = Strip(p, r, view, id, master ? mix.name : ch->name, master ? glyph::Headphones : GlyphFor(*ch), master, level, muted, tap);
        Held* h = Hold(key);
        bool dragging = ui.IsActive(id);
        if (moved) {
            if (!h) held.push_back({key}), h = &held.back();
            h->level = level, h->dirty = true, h->released = 0;
        }
        if (h && !dragging && h->released == 0) h->released = now; // let go, or a wheel notch: wave link's own value takes over shortly
        if (h && h->dirty && now - h->sentAt >= kSendEvery) { // at most so often while moving; the last move always goes
            h->sentAt = now, h->dirty = false;
            if (master) link->Post("setMix", "{\"id\":" + Quote(mix.id) + ",\"level\":" + Num(h->level) + "}");
            else link->Post("setChannel", "{\"id\":" + Quote(ch->id) + ",\"mixes\":[{\"id\":" + Quote(mix.id) + ",\"level\":" + Num(h->level) + "}]}");
            if (master) mix.level = h->level;
            else send->level = h->level;
        }
        if (tap) {
            if (master) link->Post("setMix", "{\"id\":" + Quote(mix.id) + ",\"isMuted\":" + (muted ? "false" : "true") + "}"), mix.muted = !muted;
            else link->Post("setChannel", "{\"id\":" + Quote(ch->id) + ",\"mixes\":[{\"id\":" + Quote(mix.id) + ",\"isMuted\":" + (muted ? "false" : "true") + "}]}"), send->muted = !muted;
        }
    }
    c.PopClip();
    if (scroll > 1) c.FillH({view.left, view.top, view.left + 18, view.bottom}, 0, t.card, t.card.Alpha(0));
    if (scroll < maxS - 1) c.FillH({view.right - 18, view.top, view.right, view.bottom}, 0, t.card.Alpha(0), t.card);
    if (!held.empty()) ui.Wake();
}

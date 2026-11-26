// the apps page: a start button and the apps you pick, so they can come off the taskbar
#include <algorithm>
#include <atomic>
#include <cwctype>
#include <deque>
#include <map>
#include <memory>
#include <mutex>
#include <set>
#include <string>
#include <thread>
#include <vector>

#include <windows.h>
#include <knownfolders.h>
#include <shellapi.h>
#include <shlobj.h>
#include <shobjidl.h>

#include "glyphs.h"
#include "page_sheet.h"
#include "pages.h"

namespace {

constexpr wchar_t kPinsVal[] = L"AppsPinned"; // one app a line: what to open, a tab, its name
constexpr UINT kIconPx = 64; // fetched once at this size: crisp up to 150% scale at the 40 dip we draw
constexpr float kTileW = 92, kTileH = 96, kTileGap = 8, kIconDip = 40, kHeadH = 44; // the page's grid
constexpr float kPickH = 46, kPickMinW = 210, kPickIcon = 26; // the chooser's list
constexpr float kBtnH = 28, kBadge = 20;

struct App {
    std::wstring target; // shell:AppsFolder\<id>, or a path to an exe or shortcut
    std::wstring name;
};

struct Pix {
    UINT w = 0, h = 0;
    std::vector<uint32_t> px; // premultiplied bgra
};

struct Work { // the shell is slow: it's walked on a thread of its own, never the panel's
    std::mutex lock;
    bool running = false;
    bool wantList = false, listReady = false;
    std::vector<App> list; // every app windows would let you pin, A to Z
    std::deque<std::wstring> todo; // icons to fetch
    std::map<std::wstring, std::shared_ptr<Pix>> done; // fetched; a failed one comes back empty
    bool browsing = false;
    std::vector<App> browsed; // picked in the file dialog, waiting to be added
};
std::shared_ptr<Work> work = std::make_shared<Work>();

std::vector<App> pins; // the page's apps, in order
bool loaded = false, editing = false, picking = false;
float scrollTo = 0, pickScrollTo = 0;
std::vector<App> catalog; // the chooser's list, held only while it's open
bool catalogAsked = false;
std::map<std::wstring, std::shared_ptr<Pix>> pixels; // icons this side, by target
std::set<std::wstring> asked;
std::map<std::wstring, ComPtr<ID2D1Bitmap>> bitmaps; // on the current device
ID2D1RenderTarget* owner = nullptr;

std::wstring Clean(std::wstring s) { // nothing that would break a saved line
    for (wchar_t& ch : s)
        if (ch == L'\t' || ch == L'\n' || ch == L'\r') ch = L' ';
    return s;
}

void Save() {
    std::wstring out;
    for (const App& a : pins) out += Clean(a.target) + L"\t" + Clean(a.name) + L"\n";
    sys::SaveText(kPinsVal, out.empty() ? std::wstring(L"-") : out); // empty means never set, so none is a dash
}

void Load() {
    if (loaded) return;
    loaded = true;
    std::wstring s = sys::ReadText(kPinsVal);
    size_t a = 0;
    while (a < s.size()) {
        size_t b = s.find(L'\n', a);
        if (b == std::wstring::npos) b = s.size();
        std::wstring line = s.substr(a, b - a);
        a = b + 1;
        size_t tab = line.find(L'\t');
        if (tab == std::wstring::npos || tab == 0) continue;
        pins.push_back({line.substr(0, tab), line.substr(tab + 1)});
    }
}

bool Pinned(const std::wstring& target) {
    return std::any_of(pins.begin(), pins.end(), [&](const App& a) { return _wcsicmp(a.target.c_str(), target.c_str()) == 0; });
}

void Pin(const App& a) {
    if (a.target.empty() || Pinned(a.target)) return;
    pins.push_back(a);
    Save();
}

void Unpin(const std::wstring& target) {
    pins.erase(std::remove_if(pins.begin(), pins.end(), [&](const App& a) { return _wcsicmp(a.target.c_str(), target.c_str()) == 0; }), pins.end());
    Save();
}

// --- the worker's side ---

bool Junk(const std::wstring& name, const std::wstring& id) { // what all apps lists but nobody launches from a taskbar
    std::wstring n = name, i = id;
    for (wchar_t& ch : n) ch = (wchar_t)towlower(ch);
    for (wchar_t& ch : i) ch = (wchar_t)towlower(ch);
    if (n.rfind(L"uninstall", 0) == 0 || n.find(L"uninstall ") != std::wstring::npos) return true;
    if (i.rfind(L"http://", 0) == 0 || i.rfind(L"https://", 0) == 0) return true;
    static const wchar_t* const kDocs[] = {L".url", L".txt", L".chm", L".htm", L".html", L".pdf", L".rtf", L".hlp"};
    for (const wchar_t* ext : kDocs) {
        size_t e = wcslen(ext);
        if (i.size() > e && i.compare(i.size() - e, e, ext) == 0) return true;
    }
    return false;
}

std::vector<App> ListAll() { // the start menu's all apps: desktop programs and store apps alike
    std::vector<App> out;
    ComPtr<IShellItem> folder;
    if (FAILED(SHGetKnownFolderItem(FOLDERID_AppsFolder, KF_FLAG_DEFAULT, nullptr, IID_PPV_ARGS(&folder)))) return out;
    ComPtr<IEnumShellItems> items;
    if (FAILED(folder->BindToHandler(nullptr, BHID_EnumItems, IID_PPV_ARGS(&items)))) return out;
    for (ComPtr<IShellItem> it; items->Next(1, it.ReleaseAndGetAddressOf(), nullptr) == S_OK;) {
        PWSTR name = nullptr, id = nullptr;
        if (SUCCEEDED(it->GetDisplayName(SIGDN_NORMALDISPLAY, &name)) && SUCCEEDED(it->GetDisplayName(SIGDN_PARENTRELATIVEPARSING, &id)) && name && id && *name && *id &&
            !Junk(name, id))
            out.push_back({std::wstring(L"shell:AppsFolder\\") + id, name});
        CoTaskMemFree(name);
        CoTaskMemFree(id);
    }
    std::sort(out.begin(), out.end(), [](const App& a, const App& b) { return _wcsicmp(a.name.c_str(), b.name.c_str()) < 0; });
    out.erase(std::unique(out.begin(), out.end(), [](const App& a, const App& b) { return _wcsicmp(a.target.c_str(), b.target.c_str()) == 0; }), out.end());
    return out;
}

std::shared_ptr<Pix> Fetch(const std::wstring& target) { // the icon explorer would show
    auto out = std::make_shared<Pix>();
    ComPtr<IShellItemImageFactory> f;
    constexpr wchar_t kApps[] = L"shell:AppsFolder\\";
    constexpr size_t kAppsLen = sizeof(kApps) / sizeof(wchar_t) - 1;
    HRESULT hr = SHCreateItemFromParsingName(target.c_str(), nullptr, IID_PPV_ARGS(&f));
    if (FAILED(hr) && _wcsnicmp(target.c_str(), kApps, kAppsLen) == 0) // some shells want the apps folder asked directly
        hr = SHCreateItemInKnownFolder(FOLDERID_AppsFolder, KF_FLAG_DONT_VERIFY, target.c_str() + kAppsLen, IID_PPV_ARGS(&f));
    if (FAILED(hr) || !f) return out;
    HBITMAP bmp = nullptr;
    if (FAILED(f->GetImage({(LONG)kIconPx, (LONG)kIconPx}, SIIGBF_RESIZETOFIT | SIIGBF_ICONONLY, &bmp)) || !bmp) return out;
    BITMAP bm{};
    GetObjectW(bmp, sizeof(bm), &bm);
    if (bm.bmWidth > 0 && bm.bmHeight > 0 && bm.bmWidth <= 512 && bm.bmHeight <= 512) {
        Pix p;
        p.w = (UINT)bm.bmWidth, p.h = (UINT)bm.bmHeight;
        p.px.assign((size_t)p.w * p.h, 0);
        BITMAPINFO bi{};
        bi.bmiHeader = {sizeof(BITMAPINFOHEADER), bm.bmWidth, -bm.bmHeight, 1, 32, BI_RGB}; // top-down
        HDC dc = GetDC(nullptr);
        bool ok = GetDIBits(dc, bmp, 0, p.h, p.px.data(), &bi, DIB_RGB_COLORS) == (int)p.h;
        ReleaseDC(nullptr, dc);
        if (ok) {
            bool alpha = std::any_of(p.px.begin(), p.px.end(), [](uint32_t v) { return (v >> 24) != 0; });
            for (uint32_t& v : p.px) {
                if (!alpha) {
                    v |= 0xFF000000u;
                    continue;
                }
                uint32_t a = v >> 24, r = (std::min)((v >> 16) & 0xFFu, a), g = (std::min)((v >> 8) & 0xFFu, a), b = (std::min)(v & 0xFFu, a); // a valid premultiplied pixel
                v = a << 24 | r << 16 | g << 8 | b;
            }
            *out = std::move(p);
        }
    }
    DeleteObject(bmp);
    return out;
}

void Kick() { // starts the worker if it has something to do and isn't running
    std::shared_ptr<Work> w = work;
    {
        std::lock_guard<std::mutex> g(w->lock);
        if (w->running || (!w->wantList && w->todo.empty())) return;
        w->running = true;
    }
    try {
        std::thread([w] {
            HRESULT co = CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE); // the shell wants an sta
            SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_BELOW_NORMAL);
            for (;;) {
                bool list = false;
                std::wstring job;
                {
                    std::lock_guard<std::mutex> g(w->lock);
                    if (w->wantList) list = true, w->wantList = false;
                    else if (!w->todo.empty()) job = std::move(w->todo.front()), w->todo.pop_front();
                    else {
                        w->running = false;
                        break;
                    }
                }
                if (list) {
                    std::vector<App> all = ListAll();
                    std::lock_guard<std::mutex> g(w->lock);
                    w->list = std::move(all);
                    w->listReady = true;
                    continue;
                }
                std::shared_ptr<Pix> px = Fetch(job);
                std::lock_guard<std::mutex> g(w->lock);
                w->done[job] = std::move(px);
            }
            if (SUCCEEDED(co)) CoUninitialize();
        }).detach();
    } catch (...) {
        std::lock_guard<std::mutex> g(w->lock);
        w->running = false;
    }
}

void Browse() { // any program or shortcut, the way the taskbar lets you pin one
    std::shared_ptr<Work> w = work;
    {
        std::lock_guard<std::mutex> g(w->lock);
        if (w->browsing) return;
        w->browsing = true;
    }
    try {
        std::thread([w] {
            HRESULT co = CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE);
            std::vector<App> got;
            ComPtr<IFileOpenDialog> d;
            if (SUCCEEDED(CoCreateInstance(CLSID_FileOpenDialog, nullptr, CLSCTX_INPROC_SERVER, IID_PPV_ARGS(&d)))) {
                COMDLG_FILTERSPEC types[] = {{L"Programs and shortcuts", L"*.exe;*.lnk;*.bat;*.cmd;*.appref-ms"}, {L"All files", L"*.*"}};
                d->SetFileTypes(2, types);
                d->SetTitle(L"Add to the Apps page");
                FILEOPENDIALOGOPTIONS o = 0;
                d->GetOptions(&o);
                d->SetOptions(o | FOS_ALLOWMULTISELECT | FOS_FILEMUSTEXIST | FOS_NODEREFERENCELINKS | FOS_FORCEFILESYSTEM);
                ComPtr<IShellItemArray> picked;
                if (SUCCEEDED(d->Show(nullptr)) && SUCCEEDED(d->GetResults(&picked))) {
                    DWORD n = 0;
                    picked->GetCount(&n);
                    for (DWORD i = 0; i < n; ++i) {
                        ComPtr<IShellItem> it;
                        PWSTR path = nullptr, name = nullptr;
                        if (SUCCEEDED(picked->GetItemAt(i, &it)) && SUCCEEDED(it->GetDisplayName(SIGDN_FILESYSPATH, &path)) && path) {
                            std::wstring nm;
                            if (SUCCEEDED(it->GetDisplayName(SIGDN_NORMALDISPLAY, &name)) && name) nm = name;
                            std::wstring p = path;
                            if (nm.empty()) nm = p.substr(p.find_last_of(L'\\') + 1);
                            size_t dot = nm.rfind(L'.');
                            if (dot != std::wstring::npos && dot > 0 && (_wcsicmp(nm.c_str() + dot, L".exe") == 0 || _wcsicmp(nm.c_str() + dot, L".lnk") == 0)) nm.resize(dot);
                            got.push_back({p, nm});
                        }
                        CoTaskMemFree(path);
                        CoTaskMemFree(name);
                    }
                }
            }
            if (SUCCEEDED(co)) CoUninitialize();
            std::lock_guard<std::mutex> g(w->lock);
            for (auto& a : got) w->browsed.push_back(std::move(a));
            w->browsing = false;
        }).detach();
    } catch (...) {
        std::lock_guard<std::mutex> g(w->lock);
        w->browsing = false;
    }
}

std::vector<App> TaskbarPins() { // what's pinned to the taskbar now, from the shortcuts windows keeps for it
    std::vector<App> out;
    PWSTR roaming = nullptr;
    if (FAILED(SHGetKnownFolderPath(FOLDERID_RoamingAppData, 0, nullptr, &roaming)) || !roaming) return out;
    std::wstring dir = std::wstring(roaming) + L"\\Microsoft\\Internet Explorer\\Quick Launch\\User Pinned\\TaskBar\\";
    CoTaskMemFree(roaming);
    WIN32_FIND_DATAW fd;
    HANDLE h = FindFirstFileW((dir + L"*.lnk").c_str(), &fd);
    if (h == INVALID_HANDLE_VALUE) return out;
    do {
        if (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) continue;
        std::wstring nm = fd.cFileName;
        nm.resize(nm.size() - 4); // .lnk
        out.push_back({dir + fd.cFileName, nm});
    } while (FindNextFileW(h, &fd));
    FindClose(h);
    std::sort(out.begin(), out.end(), [](const App& a, const App& b) { return _wcsicmp(a.name.c_str(), b.name.c_str()) < 0; });
    return out;
}

// --- the panel's side ---

bool Poll() { // takes in what the worker finished; true while it still has work
    std::lock_guard<std::mutex> g(work->lock);
    for (auto& d : work->done)
        if (asked.count(d.first)) pixels[d.first] = std::move(d.second); // the chooser may have closed meanwhile
    work->done.clear();
    if (work->listReady) {
        catalog = std::move(work->list);
        work->list.clear();
        work->listReady = false;
    }
    for (auto& a : work->browsed) Pin(a);
    work->browsed.clear();
    return work->running || work->wantList || !work->todo.empty();
}

void Want(const std::wstring& target) {
    if (asked.count(target)) return;
    asked.insert(target);
    {
        std::lock_guard<std::mutex> g(work->lock);
        work->todo.push_back(target);
    }
    Kick();
}

ID2D1Bitmap* Icon(Canvas& c, const std::wstring& target) {
    if (owner != c.Target()) bitmaps.clear(), owner = c.Target(); // a new device can't use the old bitmaps
    auto b = bitmaps.find(target);
    if (b != bitmaps.end()) return b->second.Get();
    auto p = pixels.find(target);
    if (p == pixels.end()) {
        Want(target);
        return nullptr;
    }
    ComPtr<ID2D1Bitmap> bmp;
    if (p->second && !p->second->px.empty()) {
        D2D1_BITMAP_PROPERTIES bp = D2D1::BitmapProperties(D2D1::PixelFormat(DXGI_FORMAT_B8G8R8A8_UNORM, D2D1_ALPHA_MODE_PREMULTIPLIED));
        c.Target()->CreateBitmap({p->second->w, p->second->h}, p->second->px.data(), p->second->w * 4, bp, &bmp);
    }
    bitmaps[target] = bmp; // a failed one is kept as nothing: it isn't asked for again
    return bmp.Get();
}

void Forget() { // the chooser closed: icons the page doesn't show go
    for (auto it = pixels.begin(); it != pixels.end();) {
        if (Pinned(it->first)) ++it;
        else bitmaps.erase(it->first), it = pixels.erase(it);
    }
    for (auto it = asked.begin(); it != asked.end();) {
        if (Pinned(*it)) ++it;
        else it = asked.erase(it);
    }
    std::lock_guard<std::mutex> g(work->lock);
    work->todo.clear();
    for (const std::wstring& t : asked)
        if (!pixels.count(t)) work->todo.push_back(t);
}

void Launch(const std::wstring& target) {
    std::wstring t = target;
    try {
        std::thread([t] {
            HRESULT co = CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE);
            AllowSetForegroundWindow(ASFW_ANY); // the panel never activates: let what starts come to the front
            SHELLEXECUTEINFOW sei{sizeof(sei)};
            sei.fMask = SEE_MASK_NOASYNC | SEE_MASK_FLAG_LOG_USAGE;
            sei.lpVerb = L"open";
            sei.lpFile = t.c_str();
            sei.nShow = SW_SHOWNORMAL;
            if (!ShellExecuteExW(&sei)) { // explorer itself knows every shell: path
                std::wstring arg = L"\"" + t + L"\"";
                ShellExecuteW(nullptr, L"open", L"explorer.exe", arg.c_str(), nullptr, SW_SHOWNORMAL);
            }
            if (SUCCEEDED(co)) CoUninitialize();
        }).detach();
    } catch (...) {
    }
}

void OpenStart() { // the windows key, as if pressed
    INPUT in[2] = {};
    in[0].type = in[1].type = INPUT_KEYBOARD;
    in[0].ki.wVk = in[1].ki.wVk = VK_LWIN;
    in[1].ki.dwFlags = KEYEVENTF_KEYUP;
    SendInput(2, in, sizeof(INPUT));
}

std::wstring Fit(Canvas& c, const std::wstring& s, Weight w, float size, float room) { // one line, cut with an ellipsis
    if (c.Measure(s, w, size) <= room) return s;
    std::wstring cut = s;
    while (!cut.empty() && c.Measure(cut + L"…", w, size) > room) cut.pop_back();
    while (!cut.empty() && cut.back() == L' ') cut.pop_back();
    return cut + L"…";
}

void WinMark(Canvas& c, D2D1_POINT_2F o, float size, Color col) { // four panes
    float g = size * 0.08f, s = (size - g) / 2;
    float x = o.x - size / 2, y = o.y - size / 2;
    c.Fill(Rect(x, y, s, s), s * 0.12f, col);
    c.Fill(Rect(x + s + g, y, s, s), s * 0.12f, col);
    c.Fill(Rect(x, y + s + g, s, s), s * 0.12f, col);
    c.Fill(Rect(x + s + g, y + s + g, s, s), s * 0.12f, col);
}

D2D1_RECT_F Clipped(const D2D1_RECT_F& r, const D2D1_RECT_F& v) { // what's of r inside the view: only that answers the mouse
    return {(std::max)(r.left, v.left), (std::max)(r.top, v.top), (std::min)(r.right, v.right), (std::min)(r.bottom, v.bottom)};
}

struct TileOut {
    bool open = false, remove = false, left = false, right = false;
};

TileOut Tile(PageCtx& p, const D2D1_RECT_F& r, const D2D1_RECT_F& view, int key, int kind, const App* app, bool first, bool last) { // kind 0 start, 1 app, 2 add
    Ui& ui = p.ui;
    Canvas& c = ui.C();
    const Theme& t = p.shell;
    TileOut out;
    bool edit = editing && kind == 1;
    D2D1_RECT_F hit = Clipped(r, view);
    if (Width(hit) <= 0 || Height(hit) <= 0) return out;
    D2D1_RECT_F xr = Rect(r.right - kBadge - 4, r.top + 4, kBadge, kBadge);
    D2D1_RECT_F lr = Rect(r.left + 4, r.bottom - kBadge - 4, kBadge, kBadge), rr = Rect(r.right - kBadge - 4, r.bottom - kBadge - 4, kBadge, kBadge);
    Ui::Hit hx{}, hl{}, hrt{};
    if (edit) { // the badges first, so a press on one is theirs and not the tile's
        hx = ui.Button(Ui::Id("ap.x", key), Clipped(xr, view));
        ui.Name(L"Remove " + app->name);
        if (!first) hl = ui.Button(Ui::Id("ap.l", key), Clipped(lr, view)), ui.Name(L"Move " + app->name + L" earlier");
        if (!last) hrt = ui.Button(Ui::Id("ap.r", key), Clipped(rr, view)), ui.Name(L"Move " + app->name + L" later");
    }
    uint32_t id = Ui::Id("ap.tile", key);
    Ui::Hit h = ui.Button(id, hit);
    ui.Name(kind == 0 ? std::wstring(L"Start") : kind == 2 ? std::wstring(L"Add apps") : app->name);
    float hov = ui.Smooth(id + 1, h.hover ? 1.0f : 0, 16), press = ui.Smooth(id + 2, h.down ? 1.0f : 0, 24);
    c.Fill(r, 16, t.text.Alpha(0.03f + 0.07f * hov + 0.04f * press));
    if (edit) c.Stroke(r, 16, t.accent.Alpha(0.35f + 0.3f * hov), 1);
    D2D1_POINT_2F o = {(r.left + r.right) / 2, r.top + 14 + kIconDip / 2 + press};
    float sz = kIconDip * (1 - 0.06f * press);
    std::wstring label;
    if (kind == 0) {
        c.Fill(Rect(o.x - sz / 2, o.y - sz / 2, sz, sz), 11, t.accent.Alpha(0.18f + 0.12f * hov));
        WinMark(c, o, sz * 0.5f, t.accentHi);
        label = L"Start";
    } else if (kind == 2) {
        c.Stroke(Rect(o.x - sz / 2, o.y - sz / 2, sz, sz), 11, t.textDim.Alpha(0.5f + 0.4f * hov), 1.2f);
        c.Icon(glyph::Add, o, 24, t.textDim.Mix(t.text, hov));
        label = L"Add";
    } else {
        ID2D1Bitmap* bmp = Icon(c, app->target);
        if (bmp) c.Image(bmp, Rect(o.x - sz / 2, o.y - sz / 2, sz, sz), 0);
        else c.Icon(glyph::Window, o, 28, t.textDim);
        label = app->name;
    }
    c.Text(Fit(c, label, Weight::Medium, 11.5f, Width(r) - 12), Rect(r.left + 6, r.top + 66, Width(r) - 12, 18), Weight::Medium, 11.5f, t.text.Mix(t.textDim, kind == 2 ? 0.5f : 0),
           Align::Center);
    if (edit) {
        auto badge = [&](const D2D1_RECT_F& b, const Ui::Hit& bh, bool danger, int dir) {
            D2D1_POINT_2F bc = {(b.left + b.right) / 2, (b.top + b.bottom) / 2};
            c.Dot(bc, kBadge / 2, (danger ? sheet::kDanger : t.track).Alpha(bh.hover ? 1.0f : 0.85f));
            if (dir == 0) c.Icon(glyph::Close, bc, 13, danger ? Color{0, 0, 0, 0.85f} : t.text);
            else { // a chevron, drawn: the symbol font here has only the one pointing right
                float s = 3.5f * (float)dir;
                c.Line({bc.x + s * 0.5f, bc.y - 4}, {bc.x - s * 0.5f, bc.y}, t.text, 1.6f);
                c.Line({bc.x - s * 0.5f, bc.y}, {bc.x + s * 0.5f, bc.y + 4}, t.text, 1.6f);
            }
        };
        badge(xr, hx, true, 0);
        if (!first) badge(lr, hl, false, 1);
        if (!last) badge(rr, hrt, false, -1);
        out.remove = hx.clicked, out.left = hl.clicked, out.right = hrt.clicked;
        return out;
    }
    out.open = h.clicked;
    return out;
}

void Grid(PageCtx& p, const D2D1_RECT_F& a) {
    Ui& ui = p.ui;
    Canvas& c = ui.C();
    const Theme& t = p.shell;
    w::Card(ui, t, a);
    c.TextSp(L"APPS", Rect(a.left + 18, a.top + 16, 120, 16), Weight::Medium, 10, t.textLow, 1.4f);
    float bx = a.right - 14;
    auto btn = [&](const char* id, const wchar_t* label, float wdt, bool primary) {
        bx -= wdt;
        bool hit = sheet::Act(ui, t, Ui::Id(id), Rect(bx, a.top + 10, wdt, kBtnH), label, primary, false, true);
        bx -= 8;
        return hit;
    };
    if (btn("ap.edit", editing ? L"Done" : L"Edit", 64, editing)) editing = !editing;
    if (!editing && btn("ap.add", L"Add apps", 84, false)) picking = true, pickScrollTo = 0;
    if (editing) c.Text(L"Arrange with the arrows, remove with the x", Rect(a.left + 70, a.top + 13, (std::max)(0.0f, bx - a.left - 78), 18), Weight::Light, 11, t.textDim);

    D2D1_RECT_F view = {a.left + 10, a.top + kHeadH, a.right - 10, a.bottom - 10};
    int n = (int)pins.size() + 2; // the start button, the apps, and the add tile
    int cols = (std::max)(1, (int)((Width(view) + kTileGap) / (kTileW + kTileGap)));
    float tw = (std::min)(kTileW + 24, (Width(view) - kTileGap * (cols - 1)) / cols); // stretched a little to fill the row
    int rows = (n + cols - 1) / cols;
    float content = rows * kTileH + (rows - 1) * kTileGap;
    float scroll = sheet::Scroll(ui, "ap.scroll", scrollTo, view, content);
    c.PushClip(view);
    int moveFrom = -1, moveTo = -1, removeAt = -1;
    for (int i = 0; i < n; ++i) {
        D2D1_RECT_F r = Rect(view.left + (i % cols) * (tw + kTileGap), view.top + (i / cols) * (kTileH + kTileGap) - scroll, tw, kTileH);
        if (r.bottom < view.top || r.top > view.bottom) continue;
        int kind = i == 0 ? 0 : i == n - 1 ? 2 : 1;
        const App* app = kind == 1 ? &pins[(size_t)(i - 1)] : nullptr;
        int key = kind == 1 ? sheet::Key(app->target) : kind == 0 ? -1 : -2;
        TileOut o = Tile(p, r, view, key, kind, app, i == 1, i == n - 2);
        if (o.open) {
            p.wantClose = true;
            if (kind == 0) OpenStart();
            else if (kind == 2) picking = true, pickScrollTo = 0, p.wantClose = false;
            else Launch(app->target);
        }
        if (o.remove) removeAt = i - 1;
        if (o.left) moveFrom = i - 1, moveTo = i - 2;
        if (o.right) moveFrom = i - 1, moveTo = i;
    }
    c.PopClip();
    sheet::Fades(c, t, view, scroll, content);
    if (ui.RClick(view)) editing = !editing; // right-click anywhere on it: arrange
    if (removeAt >= 0 && removeAt < (int)pins.size()) Unpin(pins[(size_t)removeAt].target);
    if (moveFrom >= 0 && moveTo >= 0 && moveTo < (int)pins.size()) std::swap(pins[(size_t)moveFrom], pins[(size_t)moveTo]), Save();
    if (pins.empty() && !editing) {
        float y = view.top + kTileH + 18;
        if (y + 34 < view.bottom)
            c.Text(L"Pick the apps you want here with Add apps. Anything Windows lets you pin to the taskbar can go on this page.", Rect(view.left + 8, y, Width(view) - 16, 36),
                   Weight::Light, 11.5f, t.textDim);
    }
}

void Chooser(PageCtx& p, const D2D1_RECT_F& a) {
    Ui& ui = p.ui;
    Canvas& c = ui.C();
    const Theme& t = p.shell;
    w::Card(ui, t, a);
    if (!catalogAsked) {
        catalogAsked = true;
        {
            std::lock_guard<std::mutex> g(work->lock);
            work->wantList = true;
        }
        Kick();
    }
    c.Text(L"Choose apps", Rect(a.left + 18, a.top + 12, 200, 24), Weight::Semi, 15, t.text);
    float bx = a.right - 14;
    auto btn = [&](const char* id, const wchar_t* label, float wdt, bool primary) {
        bx -= wdt;
        bool hit = sheet::Act(ui, t, Ui::Id(id), Rect(bx, a.top + 10, wdt, kBtnH), label, primary, false, true);
        bx -= 8;
        return hit;
    };
    bool done = btn("ap.done", L"Done", 70, true);
    if (btn("ap.browse", L"Browse…", 84, false)) {
        p.wantClose = true; // out of the way of the file dialog
        Browse();
    }
    if (btn("ap.import", L"Taskbar pins", 104, false))
        for (const App& a2 : TaskbarPins()) Pin(a2);
    c.Text(std::to_wstring(pins.size()) + L" on the page", Rect(a.left + 130, a.top + 15, (std::max)(0.0f, bx - a.left - 138), 18), Weight::Light, 11, t.textDim);

    D2D1_RECT_F view = {a.left + 10, a.top + kHeadH + 2, a.right - 10, a.bottom - 10};
    if (catalog.empty()) {
        sheet::Spinner(c, {(view.left + view.right) / 2, (view.top + view.bottom) / 2 - 8}, 9, t.accent, ui.Now());
        c.Text(L"Finding your apps…", Rect(view.left, (view.top + view.bottom) / 2 + 10, Width(view), 18), Weight::Light, 12, t.textDim, Align::Center);
        ui.Wake();
    } else {
        int cols = (std::max)(1, (int)((Width(view) + kTileGap) / (kPickMinW + kTileGap)));
        float cw = (Width(view) - kTileGap * (cols - 1)) / cols;
        int n = (int)catalog.size(), rows = (n + cols - 1) / cols;
        float content = rows * kPickH + (rows - 1) * 6.0f;
        float scroll = sheet::Scroll(ui, "ap.pscroll", pickScrollTo, view, content);
        c.PushClip(view);
        int toggle = -1;
        for (int i = 0; i < n; ++i) {
            D2D1_RECT_F r = Rect(view.left + (i % cols) * (cw + kTileGap), view.top + (i / cols) * (kPickH + 6) - scroll, cw, kPickH);
            if (r.bottom < view.top || r.top > view.bottom) continue;
            const App& app = catalog[(size_t)i];
            bool on = Pinned(app.target);
            uint32_t id = Ui::Id("ap.pick", sheet::Key(app.target));
            Ui::Hit h = ui.Button(id, Clipped(r, view));
            ui.Mark(Ui::Role::Toggle, on);
            ui.Name(app.name);
            float hov = ui.Smooth(id + 1, h.hover ? 1.0f : 0, 16), sel = ui.Smooth(id + 2, on ? 1.0f : 0, 16);
            c.Fill(r, 12, t.text.Alpha(0.04f + 0.05f * hov));
            if (sel > 0.01f) c.Stroke(r, 12, t.accent.Alpha(0.8f * sel), 1.5f);
            float mid = (r.top + r.bottom) / 2;
            ID2D1Bitmap* bmp = Icon(c, app.target);
            if (bmp) c.Image(bmp, Rect(r.left + 12, mid - kPickIcon / 2, kPickIcon, kPickIcon), 0);
            else c.Icon(glyph::Window, {r.left + 12 + kPickIcon / 2, mid}, 20, t.textDim);
            float tx = r.left + 12 + kPickIcon + 10;
            c.Text(Fit(c, app.name, Weight::Medium, 12, r.right - 36 - tx), Rect(tx, mid - 9, r.right - 36 - tx, 18), Weight::Medium, 12, t.text);
            D2D1_POINT_2F o = {r.right - 18, mid};
            c.Dot(o, 9, t.text.Alpha(0.1f).Mix(t.accent, sel));
            c.Icon(on ? glyph::Check : glyph::Add, o, 13, on ? t.onAccent : t.textDim);
            if (h.clicked) toggle = i;
        }
        c.PopClip();
        sheet::Fades(c, t, view, scroll, content);
        if (toggle >= 0) {
            const App& app = catalog[(size_t)toggle];
            if (Pinned(app.target)) Unpin(app.target);
            else Pin(app);
        }
    }
    if (done || ui.RClick(a)) {
        picking = false;
        catalogAsked = false;
        catalog.clear();
        catalog.shrink_to_fit();
        Forget();
    }
}

}

void DrawApps(PageCtx& p) {
    Load();
    if (Poll()) p.ui.Wake(); // icons still coming: keep drawing until they're in
    if (picking) Chooser(p, p.area);
    else Grid(p, p.area);
}

/* NearShare.exe: start the app from a Windows bundle.
 *
 * The bundle carries its own Python (bin\pythonw.exe) and GTK. This exists
 * so the Start menu, taskbar and Explorer show NearShare's name and icon
 * rather than Python's, and so Explorer's "Send to" can hand it files:
 *
 *   NearShare.exe                   the app
 *   NearShare.exe --send FILE...    the send picker for those files
 *
 * It starts pythonw and exits; it never waits on the app.
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <shellapi.h>
#include <wchar.h>

static void append_quoted(wchar_t *dst, size_t cap, const wchar_t *arg)
{
    /* Quote one argument so CommandLineToArgvW (and so Python) reads it
     * back unchanged: backslashes double only before a quote or the end. */
    size_t n = wcslen(dst);
#define PUT(c) do { if (n + 2 < cap) dst[n++] = (c); } while (0)
    PUT(L' ');
    PUT(L'"');
    for (const wchar_t *p = arg;; p++) {
        size_t slashes = 0;
        while (*p == L'\\') { slashes++; p++; }
        if (*p == 0) {
            for (size_t i = 0; i < slashes * 2; i++) PUT(L'\\');
            break;
        }
        if (*p == L'"') {
            for (size_t i = 0; i < slashes * 2 + 1; i++) PUT(L'\\');
        } else {
            for (size_t i = 0; i < slashes; i++) PUT(L'\\');
        }
        PUT(*p);
    }
    PUT(L'"');
#undef PUT
    dst[n] = 0;
}

int WINAPI wWinMain(HINSTANCE inst, HINSTANCE prev, PWSTR cmdline, int show)
{
    (void)inst; (void)prev; (void)cmdline; (void)show;
    wchar_t dir[MAX_PATH], python[MAX_PATH + 32];
    static wchar_t command[32768];
    DWORD len = GetModuleFileNameW(NULL, dir, MAX_PATH);
    if (len == 0 || len >= MAX_PATH)
        return 1;
    wchar_t *slash = wcsrchr(dir, L'\\');
    if (slash)
        *slash = 0;
    swprintf(python, MAX_PATH + 32, L"%ls\\bin\\pythonw.exe", dir);

    int argc = 0;
    wchar_t **argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    int send = argc > 1 && wcscmp(argv[1], L"--send") == 0;
    swprintf(command, 32768, L"\"%ls\" -m %ls", python,
             send ? L"nearshare.cli send-picker" : L"nearshare");
    for (int i = send ? 2 : 1; i < argc; i++)
        append_quoted(command, 32768, argv[i]);

    STARTUPINFOW si = { sizeof si };
    PROCESS_INFORMATION pi;
    if (!CreateProcessW(python, command, NULL, NULL, FALSE, 0, NULL, dir, &si, &pi)) {
        MessageBoxW(NULL, L"NearShare could not start its Python runtime "
                    L"(bin\\pythonw.exe is missing). Reinstall NearShare.",
                    L"NearShare", MB_ICONERROR);
        return 1;
    }
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    LocalFree(argv);
    return 0;
}

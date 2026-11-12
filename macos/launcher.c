/*
 * The app's executable. It does one thing: start the Python that is bundled inside the app, in this
 * process, and run j1x0_macapp. Running Python in-process (not as a child) keeps this the app's one
 * process, so the Dock icon, the menu bar name and macOS's idea of "the app" are all right.
 *
 *   J1x0 Card Printer.app/Contents/MacOS/CardPrinter     this program
 *   J1x0 Card Printer.app/Contents/Resources/python/     the relocatable Python (lib/libpython3.x.dylib, ...)
 *   J1x0 Card Printer.app/Contents/Resources/app/        j1x0_macapp.py
 */
#include <dlfcn.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifndef PYTHON_LIB
#define PYTHON_LIB "libpython3.13.dylib"
#endif

static void fail(const char *what, const char *detail) {
    fprintf(stderr, "J1x0 Card Printer: %s%s%s\n", what, detail ? ": " : "", detail ? detail : "");
    exit(1);
}

int main(int argc, char **argv) {
    char exe[PATH_MAX], real[PATH_MAX];
    uint32_t size = sizeof exe;
    if (_NSGetExecutablePath(exe, &size) != 0 || !realpath(exe, real))
        fail("cannot find my own path", NULL);

    /* real = <app>/Contents/MacOS/CardPrinter  ->  contents = <app>/Contents */
    char contents[PATH_MAX];
    strlcpy(contents, real, sizeof contents);
    for (int slashes = 0; slashes < 2; slashes++) {
        char *slash = strrchr(contents, '/');
        if (!slash) fail("unexpected app layout", real);
        *slash = '\0';
    }

    char home[PATH_MAX], lib[PATH_MAX], appdir[PATH_MAX];
    snprintf(home, sizeof home, "%s/Resources/python", contents);
    snprintf(lib, sizeof lib, "%s/lib/%s", home, PYTHON_LIB);
    snprintf(appdir, sizeof appdir, "%s/Resources/app", contents);

    setenv("PYTHONHOME", home, 1);
    setenv("PYTHONPATH", appdir, 1);
    setenv("PYTHONDONTWRITEBYTECODE", "1", 1);   /* the app bundle is signed: never write into it */
    setenv("PYTHONNOUSERSITE", "1", 1);
    setenv("J1X0_APP_CONTENTS", contents, 1);

    void *python = dlopen(lib, RTLD_NOW | RTLD_GLOBAL);
    if (!python) fail("cannot load Python", dlerror());
    int (*py_bytes_main)(int, char **) = (int (*)(int, char **))dlsym(python, "Py_BytesMain");
    if (!py_bytes_main) fail("Python has no Py_BytesMain", dlerror());

    /* python -s -X utf8 -m j1x0_macapp [whatever we were given, e.g. --self-test] */
    char *args[64] = {argv[0], "-s", "-X", "utf8", "-m", "j1x0_macapp", NULL};
    int n = 6;
    for (int i = 1; i < argc && n < 62; i++) {
        if (strncmp(argv[i], "-psn_", 5) == 0) continue;     /* old Finder launch argument */
        args[n++] = argv[i];
    }
    args[n] = NULL;
    return py_bytes_main(n, args);
}

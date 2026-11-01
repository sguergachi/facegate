/*
 * FaceGate's island, as a native executable.
 *
 * KWin lets only trusted executables draw over the lock screen: the ones
 * whose desktop file lists kde_lockscreen_overlay_v1 in
 * X-KDE-Wayland-Interfaces. It identifies a client by /proc/PID/exe, which
 * for a Python script is the interpreter itself, so granting it there would
 * trust every Python program. This root-owned launcher embeds Python and only
 * ever runs FaceGate's installed island (isolated from PYTHON* variables and
 * user site-packages), so the grant covers FaceGate alone.
 *
 * It also exports facegate_allow_over_lock_screen() for the island to call,
 * through ctypes, each time its layer surface is created.
 */

#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <gdk/wayland/gdkwayland.h>
#include <stdbool.h>
#include <string.h>
#include <wayland-client.h>

#include "kde-lockscreen-overlay-v1-client-protocol.h"

static struct kde_lockscreen_overlay_v1 *overlay;
static bool looked_up;

static void registry_global(void *data, struct wl_registry *registry, uint32_t name,
                            const char *interface, uint32_t version)
{
    (void)data;
    (void)version;
    if (strcmp(interface, kde_lockscreen_overlay_v1_interface.name) == 0)
        overlay = wl_registry_bind(registry, name, &kde_lockscreen_overlay_v1_interface, 1);
}

static void registry_global_remove(void *data, struct wl_registry *registry, uint32_t name)
{
    (void)data;
    (void)registry;
    (void)name;
}

static const struct wl_registry_listener registry_listener = {
    registry_global,
    registry_global_remove,
};

/* KWin only advertises the global to executables it trusts, so on any other
 * compositor, or without the desktop file, this quietly does nothing. The
 * lookup uses its own event queue so GDK's never sees these events. */
static void look_up_overlay(struct wl_display *display)
{
    struct wl_event_queue *queue;
    struct wl_display *wrapper;
    struct wl_registry *registry;

    looked_up = true;
    queue = wl_display_create_queue(display);
    wrapper = wl_proxy_create_wrapper(display);
    if (queue == NULL || wrapper == NULL)
        return;
    wl_proxy_set_queue((struct wl_proxy *)wrapper, queue);
    registry = wl_display_get_registry(wrapper);
    wl_proxy_wrapper_destroy(wrapper);
    wl_registry_add_listener(registry, &registry_listener, NULL);
    (void)wl_display_roundtrip_queue(display, queue);
    wl_registry_destroy(registry);
    /* The queue stays alive: the overlay proxy was created on it. */
}

/* Mark the surface as allowed above the lock screen. KWin requires this
 * before the surface's first buffer, so call it while mapping. Returns 1 when
 * the request was sent. */
__attribute__((visibility("default"))) int facegate_allow_over_lock_screen(GdkSurface *surface)
{
    struct wl_display *display;
    struct wl_surface *wl_surface;

    if (surface == NULL || !GDK_IS_WAYLAND_SURFACE(surface))
        return 0;
    display = gdk_wayland_display_get_wl_display(gdk_surface_get_display(surface));
    wl_surface = gdk_wayland_surface_get_wl_surface(surface);
    if (display == NULL || wl_surface == NULL)
        return 0;
    if (!looked_up)
        look_up_overlay(display);
    if (overlay == NULL)
        return 0;
    kde_lockscreen_overlay_v1_allow(overlay, wl_surface);
    (void)wl_display_flush(display);
    return 1;
}

#ifndef FACEGATE_LIBDIR
#define FACEGATE_LIBDIR "/usr/local/lib/facegate"
#endif

static const char island[] =
    "import sys\n"
    "sys.path.insert(0, '" FACEGATE_LIBDIR "')\n"
    "from facegate.edge_hud import main\n"
    "raise SystemExit(main())\n";

int main(int argc, char **argv)
{
    char **python_argv = calloc((size_t)argc + 4, sizeof(char *));

    if (python_argv == NULL)
        return 1;
    python_argv[0] = argv[0];
    python_argv[1] = "-I";
    python_argv[2] = "-c";
    python_argv[3] = (char *)island;
    for (int index = 1; index < argc; index++)
        python_argv[index + 3] = argv[index];
    return Py_BytesMain(argc + 3, python_argv);
}

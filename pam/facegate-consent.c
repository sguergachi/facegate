/*
 * FaceGate's deliberately small PAM consent and notification bridge.
 *
 * This module never performs biometric authentication.  In "consent" mode it
 * returns PAM_SUCCESS only when the desktop HUD approved starting a scan.  The
 * PAM stack installed by FaceGate treats that result as "continue to Gaze",
 * never as authentication success.  The notification modes always return
 * PAM_IGNORE and are best-effort UI updates only.
 *
 * "greeter" mode serves the login screen, where submitting the form is the
 * request.  It takes the password the greeter already holds: a typed one goes
 * on to the normal password stack, an empty one starts the face scan.  Either
 * way the result only picks the next step, never the verdict.
 *
 * "scanning" mode serves the lock screen's biometric slot, which KScreenLocker
 * starts by itself: it only tells the island a scan is starting.
 *
 * "hud_user=NAME" sends HUD messages to NAME's island instead of the PAM
 * user's; the login screen's island runs as the greeter's own user.
 */

#define _GNU_SOURCE

#include <errno.h>
#include <pwd.h>
#include <security/pam_appl.h>
#include <security/pam_ext.h>
#include <security/pam_modules.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/types.h>
#include <sys/un.h>
#include <syslog.h>
#include <time.h>
#include <unistd.h>

#define FACEGATE_SOCKET "facegate.sock"
#define FACEGATE_MAX_MESSAGE 4096
#define FACEGATE_CONSENT_SECONDS 12
#define FACEGATE_PASSWORD_SECONDS 60
#define FACEGATE_SCANNED_DATA "facegate.scanned"
#define FACEGATE_PASSWORD_ASKED_DATA "facegate.password-asked"
#ifndef PAM_MAX_RESP_SIZE
#define PAM_MAX_RESP_SIZE 512
#endif

static char scanned_marker;
static char password_asked_marker;

static bool has_marker(pam_handle_t *pamh, const char *name, const char *marker)
{
    const void *data = NULL;

    return pam_get_data(pamh, name, &data) == PAM_SUCCESS && data == marker;
}

/* Whether this attempt handed a face scan to Gaze. Only then can a failure
 * mean "Not recognized"; a declined, skipped or cancelled scan must not say so.
 * sudo retries on the same handle, so every attempt sets it afresh. */
static void set_scanned(pam_handle_t *pamh, bool scanned)
{
    (void)pam_set_data(pamh, FACEGATE_SCANNED_DATA, scanned ? &scanned_marker : NULL, NULL);
}

static void log_message(const char *message)
{
    openlog("facegate", LOG_PID, LOG_AUTH);
    syslog(LOG_INFO, "%s", message);
    closelog();
}

static const char *module_mode(int argc, const char **argv)
{
    return argc > 0 && argv != NULL && argv[0] != NULL ? argv[0] : "consent";
}

/* The island to talk to: NAME from "hud_user=NAME", else the PAM user. */
static uid_t hud_uid(int argc, const char **argv, uid_t user_uid)
{
    for (int index = 1; index < argc && argv != NULL; index++) {
        if (argv[index] != NULL && strncmp(argv[index], "hud_user=", 9) == 0) {
            const struct passwd *entry = getpwnam(argv[index] + 9);
            return entry != NULL ? entry->pw_uid : (uid_t)-1;
        }
    }
    return user_uid;
}

static bool local_request(pam_handle_t *pamh)
{
    const void *item = NULL;
    const char *rhost;

    /* sudo commonly leaves PAM_RHOST empty over SSH, so also reject the
     * environment markers. Gaze repeats its own independent remote check. */
    if (getenv("SSH_CONNECTION") != NULL || getenv("SSH_CLIENT") != NULL ||
        getenv("SSHD_OPTS") != NULL)
        return false;
    if (pam_get_item(pamh, PAM_RHOST, &item) != PAM_SUCCESS || item == NULL)
        return true;
    rhost = item;
    return rhost[0] == '\0' || strcmp(rhost, "localhost") == 0 ||
           strcmp(rhost, "localhost.localdomain") == 0 ||
           strcmp(rhost, "127.0.0.1") == 0 || strcmp(rhost, "::1") == 0;
}

static int user_context(pam_handle_t *pamh, const char **username, uid_t *uid,
                        const char **service)
{
    const void *item = NULL;
    struct passwd *entry;

    if (pam_get_user(pamh, username, NULL) != PAM_SUCCESS || *username == NULL ||
        (*username)[0] == '\0')
        return PAM_AUTHINFO_UNAVAIL;
    entry = getpwnam(*username);
    if (entry == NULL)
        return PAM_AUTHINFO_UNAVAIL;
    *uid = entry->pw_uid;

    *service = "unknown";
    if (pam_get_item(pamh, PAM_SERVICE, &item) == PAM_SUCCESS && item != NULL)
        *service = item;
    return PAM_SUCCESS;
}

static void safe_token(const char *input, char *output, size_t size)
{
    size_t used = 0;

    if (size == 0)
        return;
    while (input != NULL && *input != '\0' && used + 1 < size) {
        const unsigned char ch = (unsigned char)*input++;
        if ((ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z') ||
            (ch >= '0' && ch <= '9') || ch == '.' || ch == '_' || ch == '-')
            output[used++] = (char)ch;
    }
    output[used] = '\0';
}

static void request_id(char *output, size_t size)
{
    FILE *random = fopen("/proc/sys/kernel/random/uuid", "re");

    if (random != NULL) {
        if (fgets(output, (int)size, random) != NULL) {
            output[strcspn(output, "\r\n")] = '\0';
            fclose(random);
            return;
        }
        fclose(random);
    }
    (void)snprintf(output, size, "%ld-%ld", (long)getpid(), (long)time(NULL));
}

static int connect_hud(uid_t uid, int timeout_seconds)
{
    char path[sizeof(((struct sockaddr_un *)0)->sun_path)];
    struct sockaddr_un address = {0};
    struct timeval timeout = {.tv_sec = timeout_seconds, .tv_usec = 0};
    struct ucred peer = {0};
    socklen_t peer_size = sizeof(peer);
    struct stat metadata;
    int fd;

    if (snprintf(path, sizeof(path), "/run/user/%lu/%s", (unsigned long)uid,
                 FACEGATE_SOCKET) >= (int)sizeof(path))
        return -1;
    if (lstat(path, &metadata) != 0 || !S_ISSOCK(metadata.st_mode) ||
        metadata.st_uid != uid || (metadata.st_mode & 0077) != 0)
        return -1;

    fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (fd < 0)
        return -1;
    (void)setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
    (void)setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
    address.sun_family = AF_UNIX;
    (void)snprintf(address.sun_path, sizeof(address.sun_path), "%s", path);
    if (connect(fd, (struct sockaddr *)&address, sizeof(address)) != 0) {
        close(fd);
        return -1;
    }
    /* Close the lstat/connect race: the process accepting the connection must
     * be running as the PAM user even if the socket pathname was exchanged. */
    if (getsockopt(fd, SOL_SOCKET, SO_PEERCRED, &peer, &peer_size) != 0 ||
        peer_size != sizeof(peer) || peer.uid != uid) {
        close(fd);
        return -1;
    }
    return fd;
}

static bool send_all(int fd, const char *message, size_t length)
{
    size_t sent = 0;

    while (sent < length) {
        const ssize_t result = send(fd, message + sent, length - sent, MSG_NOSIGNAL);
        if (result <= 0)
            return false;
        sent += (size_t)result;
    }
    return true;
}

/* Send one request to the island and read its one-line answer to that same
 * request into reply. The answer may hold a password: callers wipe it. */
static bool ask_hud(uid_t uid, const char *phase, const char *service,
                    const char *username, int seconds, bool retry, char *reply,
                    size_t size)
{
    char id[64] = {0};
    char service_token[64] = {0};
    char username_token[64] = {0};
    char message[512];
    char expected_id[96];
    size_t received = 0;
    int length;
    int fd;

    request_id(id, sizeof(id));
    safe_token(service, service_token, sizeof(service_token));
    safe_token(username, username_token, sizeof(username_token));
    length = snprintf(message, sizeof(message),
                      "{\"id\":\"%s\",\"phase\":\"%s\","
                      "\"service\":\"%s\",\"username\":\"%s\","
                      "\"timeout_ms\":%d,\"error\":%s}\n",
                      id, phase, service_token, username_token, seconds * 1000,
                      retry ? "true" : "false");
    if (length <= 0 || length >= (int)sizeof(message) ||
        snprintf(expected_id, sizeof(expected_id), "\"id\":\"%s\"", id) >=
            (int)sizeof(expected_id))
        return false;

    fd = connect_hud(uid, seconds + 2);
    if (fd < 0)
        return false;
    if (!send_all(fd, message, (size_t)length)) {
        close(fd);
        return false;
    }
    /* A stream socket may deliver the line in pieces. */
    while (received + 1 < size && memchr(reply, '\n', received) == NULL) {
        const ssize_t got = recv(fd, reply + received, size - 1 - received, 0);
        if (got <= 0)
            break;
        received += (size_t)got;
    }
    close(fd);
    reply[received] = '\0';
    if (memchr(reply, '\n', received) == NULL || strstr(reply, expected_id) == NULL) {
        explicit_bzero(reply, size);
        return false;
    }
    return true;
}

static bool consent_from_hud(uid_t uid, const char *service, const char *username)
{
    char reply[FACEGATE_MAX_MESSAGE + 1];

    if (!ask_hud(uid, "approval", service, username, FACEGATE_CONSENT_SECONDS, false,
                 reply, sizeof(reply)))
        return false;
    return strstr(reply, "\"approved\":true") != NULL;
}

/* A scan is starting; the service tells the island which screen it is on. */
static void notify_scanning(uid_t uid, const char *service)
{
    char service_token[64] = {0};
    char message[128];
    int length;
    int fd;

    safe_token(service, service_token, sizeof(service_token));
    length = snprintf(message, sizeof(message),
                      "{\"phase\":\"scanning\",\"service\":\"%s\"}\n", service_token);
    if (length <= 0 || length >= (int)sizeof(message))
        return;
    fd = connect_hud(uid, 1);
    if (fd < 0)
        return;
    (void)send_all(fd, message, (size_t)length);
    close(fd);
}

static bool put_byte(char *out, size_t size, size_t *used, unsigned int byte)
{
    if (*used + 1 >= size)
        return false;
    out[(*used)++] = (char)byte;
    return true;
}

static bool read_hex4(const char *text, unsigned int *value)
{
    *value = 0;
    for (int index = 0; index < 4; index++) {
        const char ch = text[index];
        *value <<= 4;
        if (ch >= '0' && ch <= '9')
            *value |= (unsigned int)(ch - '0');
        else if (ch >= 'a' && ch <= 'f')
            *value |= (unsigned int)(ch - 'a' + 10);
        else if (ch >= 'A' && ch <= 'F')
            *value |= (unsigned int)(ch - 'A' + 10);
        else
            return false;
    }
    return true;
}

/* Decode the JSON string value of "key" as UTF-8, including \u escapes, so a
 * password with any character reaches pam_unix exactly as it was typed.
 * Anything malformed, or a NUL that would cut the password short, fails. */
static bool json_string(const char *json, const char *key, char *out, size_t size)
{
    char needle[40];
    const char *cursor;
    size_t used = 0;
    unsigned int code;

    if (size == 0 ||
        snprintf(needle, sizeof(needle), "\"%s\":\"", key) >= (int)sizeof(needle))
        return false;
    cursor = strstr(json, needle);
    if (cursor == NULL)
        return false;
    cursor += strlen(needle);
    while (*cursor != '"') {
        if (*cursor == '\0')
            return false;
        if (*cursor != '\\') {
            if (!put_byte(out, size, &used, (unsigned char)*cursor++))
                return false;
            continue;
        }
        cursor++;
        switch (*cursor++) {
        case '"': code = '"'; break;
        case '\\': code = '\\'; break;
        case '/': code = '/'; break;
        case 'b': code = '\b'; break;
        case 'f': code = '\f'; break;
        case 'n': code = '\n'; break;
        case 'r': code = '\r'; break;
        case 't': code = '\t'; break;
        case 'u':
            if (!read_hex4(cursor, &code))
                return false;
            cursor += 4;
            if (code >= 0xD800 && code <= 0xDBFF) {
                unsigned int low;

                if (cursor[0] != '\\' || cursor[1] != 'u' || !read_hex4(cursor + 2, &low) ||
                    low < 0xDC00 || low > 0xDFFF)
                    return false;
                cursor += 6;
                code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00);
            } else if (code >= 0xDC00 && code <= 0xDFFF) {
                return false;
            }
            break;
        default:
            return false;
        }
        if (code == 0)
            return false;
        if (code < 0x80) {
            if (!put_byte(out, size, &used, code))
                return false;
        } else if (code < 0x800) {
            if (!put_byte(out, size, &used, 0xC0 | (code >> 6)) ||
                !put_byte(out, size, &used, 0x80 | (code & 0x3F)))
                return false;
        } else if (code < 0x10000) {
            if (!put_byte(out, size, &used, 0xE0 | (code >> 12)) ||
                !put_byte(out, size, &used, 0x80 | ((code >> 6) & 0x3F)) ||
                !put_byte(out, size, &used, 0x80 | (code & 0x3F)))
                return false;
        } else if (!put_byte(out, size, &used, 0xF0 | (code >> 18)) ||
                   !put_byte(out, size, &used, 0x80 | ((code >> 12) & 0x3F)) ||
                   !put_byte(out, size, &used, 0x80 | ((code >> 6) & 0x3F)) ||
                   !put_byte(out, size, &used, 0x80 | (code & 0x3F))) {
            return false;
        }
    }
    out[used] = '\0';
    return true;
}

/* The password typed into the island, or false to leave sudo its own prompt.
 * retry makes the island say the last password was wrong. */
static bool password_from_hud(uid_t uid, const char *service, const char *username,
                              bool retry, char *password, size_t size)
{
    char reply[FACEGATE_MAX_MESSAGE + 1];
    bool typed = false;

    if (ask_hud(uid, "password", service, username, FACEGATE_PASSWORD_SECONDS, retry,
                reply, sizeof(reply)))
        typed = json_string(reply, "password", password, size) && password[0] != '\0';
    explicit_bzero(reply, sizeof(reply));
    if (!typed)
        explicit_bzero(password, size);
    return typed;
}

static void notify_result(uid_t uid, const char *result)
{
    char message[64];
    const int length = snprintf(message, sizeof(message),
                                "{\"phase\":\"result\",\"result\":\"%s\"}\n", result);
    int fd;

    if (length <= 0 || length >= (int)sizeof(message))
        return;
    fd = connect_hud(uid, 1);
    if (fd < 0)
        return;
    (void)send_all(fd, message, (size_t)length);
    close(fd);
}

/* Whether the greeter was submitted with a password.  The greeter answers this
 * prompt with what was typed, exactly as it answers pam_unix; a typed password
 * is kept as PAM_AUTHTOK for pam_unix (try_first_pass) and pam_kwallet5. */
static bool greeter_password_typed(pam_handle_t *pamh)
{
    const void *item = NULL;
    char *response = NULL;
    bool typed;

    if (pam_get_item(pamh, PAM_AUTHTOK, &item) == PAM_SUCCESS && item != NULL)
        return ((const char *)item)[0] != '\0';
    if (pam_prompt(pamh, PAM_PROMPT_ECHO_OFF, &response, "Password: ") != PAM_SUCCESS ||
        response == NULL)
        return false;
    typed = response[0] != '\0';
    if (typed)
        typed = pam_set_item(pamh, PAM_AUTHTOK, response) == PAM_SUCCESS;
    explicit_bzero(response, strlen(response));
    free(response);
    return typed;
}

PAM_EXTERN int pam_sm_authenticate(pam_handle_t *pamh, int flags, int argc,
                                   const char **argv)
{
    const char *username;
    const char *service;
    const char *mode = module_mode(argc, argv);
    uid_t uid;
    int context;

    (void)flags;
    context = user_context(pamh, &username, &uid, &service);
    if (context != PAM_SUCCESS)
        return strcmp(mode, "consent") == 0 || strcmp(mode, "greeter") == 0 ? context
                                                                            : PAM_IGNORE;
    const uid_t hud = hud_uid(argc, argv, uid);

    if (strcmp(mode, "notify-failure") == 0) {
        char password[PAM_MAX_RESP_SIZE];

        if (has_marker(pamh, FACEGATE_SCANNED_DATA, &scanned_marker))
            notify_result(hud, "failure");
        /* Polkit collects the password through its agent session and the
         * greeter through its own field. sudo has nowhere else to type, so
         * the island takes it and pam_unix (try_first_pass) checks it. sudo
         * retries on the same handle, so a second ask means the first failed. */
        if (strcmp(service, "sudo") == 0 && hud == uid &&
            password_from_hud(uid, service, username,
                              has_marker(pamh, FACEGATE_PASSWORD_ASKED_DATA, &password_asked_marker),
                              password, sizeof(password))) {
            (void)pam_set_item(pamh, PAM_AUTHTOK, password);
            (void)pam_set_data(pamh, FACEGATE_PASSWORD_ASKED_DATA, &password_asked_marker, NULL);
        }
        explicit_bzero(password, sizeof(password));
        return PAM_IGNORE;
    }
    if (strcmp(mode, "notify-success") == 0) {
        notify_result(hud, "success");
        return PAM_IGNORE;
    }
    if (strcmp(mode, "greeter") == 0) {
        set_scanned(pamh, false);
        if (!local_request(pamh) || greeter_password_typed(pamh))
            return PAM_AUTHINFO_UNAVAIL;
        log_message("login submitted without a password; delegating to Gaze");
        set_scanned(pamh, true);
        notify_scanning(hud, service);
        return PAM_SUCCESS;
    }
    if (strcmp(mode, "scanning") == 0) {
        const bool local = local_request(pamh);

        set_scanned(pamh, local);
        if (local)
            notify_scanning(hud, service);
        return PAM_IGNORE;
    }
    set_scanned(pamh, false);
    if (strcmp(mode, "consent") != 0 || !local_request(pamh))
        return PAM_AUTHINFO_UNAVAIL;
    /* After a wrong password, ask for the password again, not the face. */
    if (has_marker(pamh, FACEGATE_PASSWORD_ASKED_DATA, &password_asked_marker))
        return PAM_AUTHINFO_UNAVAIL;

    if (!consent_from_hud(uid, service, username)) {
        log_message("face scan was not approved; using password authentication");
        return PAM_AUTHINFO_UNAVAIL;
    }
    log_message("face scan approved; delegating authentication to Gaze");
    set_scanned(pamh, true);
    notify_scanning(uid, service);
    return PAM_SUCCESS;
}

PAM_EXTERN int pam_sm_setcred(pam_handle_t *pamh, int flags, int argc,
                              const char **argv)
{
    (void)pamh;
    (void)flags;
    (void)argc;
    (void)argv;
    return PAM_IGNORE;
}

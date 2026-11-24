#!/bin/sh
set -eu

workspace=/workspace/minipod-codex
rootfs=$workspace/rootfs
image=minipod-codex:0.167.0
max_slots=16

slot_user() {
    if [ "$1" -eq 0 ]; then printf 'codexec'; else printf 'codexec%s' "$1"; fi
}

slot_home() {
    if [ "$1" -eq 0 ]; then printf '/workspace/codexec'; else printf '/workspace/slots/%s' "$1"; fi
}

slot_uid() {
    printf '%s' "$((1100 + $1))"
}

slot_subuid() {
    printf '%s' "$((165536 + $1 * 65536))"
}

require_uint() {
    case "$1" in
        ''|*[!0-9]*) echo "Invalid numeric argument: $1" >&2; exit 64 ;;
    esac
}

seed_slot_home() {
    s_home=$1
    s_uid=$2
    if [ -d "$rootfs/home/node" ]; then
        for dotfile in .bashrc .profile .bash_logout; do
            if [ -f "$rootfs/home/node/$dotfile" ] && [ ! -f "$s_home/home/$dotfile" ]; then
                install -m 644 -o "$s_uid" -g "$s_uid" "$rootfs/home/node/$dotfile" "$s_home/home/$dotfile"
            fi
        done
    fi
}

ensure_slot_account() {
    s=$1
    u=$(slot_user "$s")
    uid=$(slot_uid "$s")
    h=$(slot_home "$s")
    sub=$(slot_subuid "$s")
    id "$u" >/dev/null 2>&1 || adduser -D -H -u "$uid" -h "$h" -s /bin/sh "$u"
    install -d -m 755 /workspace/slots
    install -d -m 700 -o "$uid" -g "$uid" "$h" "$h/run" "$h/cache" "$h/work" "$h/home" "$h/overlay-storage"
    rm -rf "$h/.local/share/containers/storage"
    for file in /etc/subuid /etc/subgid; do
        grep -q "^$u:" "$file" || printf '%s:%s:65536\n' "$u" "$sub" >> "$file"
    done
    seed_slot_home "$h" "$uid"
}

rootfs_present() {
    [ -x "$rootfs/usr/local/bin/node" ] && { [ -L "$rootfs/usr/local/bin/codex" ] || [ -x "$rootfs/usr/local/bin/codex" ]; }
}

ensure_rootfs_etc() {
    target_dir=$1
    mkdir -p "$target_dir/etc" "$target_dir/run" "$target_dir/tmp" "$target_dir/home/node" "$target_dir/workspace"
    [ -L "$target_dir/etc/mtab" ] || ln -sfn /proc/mounts "$target_dir/etc/mtab"
    for stub_file in etc/hosts etc/hostname etc/resolv.conf run/.containerenv; do
        if [ ! -e "$target_dir/$stub_file" ]; then
            : > "$target_dir/$stub_file"
            chmod 644 "$target_dir/$stub_file"
        fi
    done
}

normalize_rootfs_tree() {
    target_dir=$1
    ensure_rootfs_etc "$target_dir"
    chown -hR 0:0 "$target_dir"
    find "$target_dir" ! -type l -exec chmod u+rwX,go+rX {} +
}

migrate_existing_image_to_rootfs() {
    if rootfs_present; then
        ensure_rootfs_etc "$rootfs"
        return 0
    fi
    legacy_store=/workspace/codexec/overlay-storage
    if [ ! -d "$legacy_store/overlay-images" ]; then
        return 0
    fi
    has_img=$(su -s /bin/sh codexec -c "cd /workspace/codexec; export PATH='$workspace/helpers':\$PATH XDG_RUNTIME_DIR=/workspace/codexec/run; podman --root '$legacy_store' --storage-driver overlay --cgroup-manager cgroupfs image exists '$image' && echo yes || echo no" 2>/dev/null || echo no)
    if [ "$has_img" != "yes" ]; then
        return 0
    fi
    rm -rf "$workspace/rootfs.tmp"
    mkdir -p "$workspace/rootfs.tmp"
    cid=$(su -s /bin/sh codexec -c "cd /workspace/codexec; export PATH='$workspace/helpers':\$PATH XDG_RUNTIME_DIR=/workspace/codexec/run; if [ -f /workspace/codexec/run/libpod/tmp/pause.pid ] && ! kill -0 \"\$(cat /workspace/codexec/run/libpod/tmp/pause.pid 2>/dev/null)\" 2>/dev/null; then podman --root '$legacy_store' --storage-driver overlay --cgroup-manager cgroupfs system migrate >/dev/null 2>&1 || true; fi; podman --root '$legacy_store' --storage-driver overlay --cgroup-manager cgroupfs create --network none '$image'")
    su -s /bin/sh codexec -c "cd /workspace/codexec; export PATH='$workspace/helpers':\$PATH XDG_RUNTIME_DIR=/workspace/codexec/run; podman --root '$legacy_store' --storage-driver overlay --cgroup-manager cgroupfs export '$cid'" | tar -xf - -C "$workspace/rootfs.tmp"
    su -s /bin/sh codexec -c "cd /workspace/codexec; export PATH='$workspace/helpers':\$PATH XDG_RUNTIME_DIR=/workspace/codexec/run; podman --root '$legacy_store' --storage-driver overlay --cgroup-manager cgroupfs rm -f '$cid' >/dev/null 2>&1 || true; podman --root '$legacy_store' --storage-driver overlay --cgroup-manager cgroupfs container prune -f >/dev/null 2>&1 || true; podman --root '$legacy_store' --storage-driver overlay --cgroup-manager cgroupfs rmi '$image' >/dev/null 2>&1 || true"
    normalize_rootfs_tree "$workspace/rootfs.tmp"
    rm -rf "$rootfs"
    mv "$workspace/rootfs.tmp" "$rootfs"
}

run_in_slot() {
    s=$1
    subcmd=$2
    prune_stopped=${3:-0}
    u=$(slot_user "$s")
    h=$(slot_home "$s")
    podman_cmd="podman --root $h/overlay-storage --storage-driver overlay --cgroup-manager cgroupfs"
    pause_pid_file="$h/run/libpod/tmp/pause.pid"
    prune_cmd=":"
    if [ "$prune_stopped" = "1" ]; then
        prune_cmd="$podman_cmd container prune -f >/dev/null 2>&1 || true"
    fi
    su -s /bin/sh "$u" -c "cd '$h'; export HOME='$h' XDG_CACHE_HOME='$h/cache' PATH='$workspace/helpers':\$PATH XDG_RUNTIME_DIR='$h/run'; if [ -f '$pause_pid_file' ] && ! kill -0 \"\$(cat '$pause_pid_file' 2>/dev/null)\" 2>/dev/null; then $podman_cmd system migrate >/dev/null 2>&1 || true; $podman_cmd container prune -f >/dev/null 2>&1 || true; fi; $prune_cmd; exec $podman_cmd $subcmd"
}

action=${1:-}
case "$action" in
    init|verify|check|load|exec) ;;
    *) exit 64 ;;
esac

# The guest supervisor owns the cgroup. Podman can read its controllers.
group=$(cut -d: -f3 /proc/self/cgroup)
case "$action" in
    check|exec)
        case "${group%/*}" in
            ""|/init) ;;
            *) echo "Unexpected Mini-pod exec cgroup parent" >&2; exit 125 ;;
        esac
        case "${group##*/}" in
            minipod-exec-*) ;;
            *) echo "Missing per-request Mini-pod exec cgroup" >&2; exit 125 ;;
        esac
        leaf="/sys/fs/cgroup$group"
        if [ ! -f "$leaf/pids.max" ]; then
            echo +pids > /sys/fs/cgroup/cgroup.subtree_control
            if [ "${group%/*}" = /init ]; then
                echo +pids > /sys/fs/cgroup/init/cgroup.subtree_control
            fi
        fi
        read -r limit < "$leaf/pids.max"
        if [ "$limit" = max ] || [ "$limit" -gt 128 ]; then
            echo 128 > "$leaf/pids.max"
        fi
        ;;
esac
[ ! -d /sys/fs/cgroup/init ] || chmod 755 /sys/fs/cgroup/init
chmod 755 "/sys/fs/cgroup$group"

container_env="--env PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin --env HOME=/home/node --env NODE_VERSION=24.22.1 --env YARN_VERSION=1.22.22"
container_sec="--rm --cgroups disabled --network none --no-hosts --cap-drop ALL --security-opt no-new-privileges --userns keep-id:uid=1000,gid=1000 --user 1000:1000 --workdir /workspace --tmpfs /tmp:rw,nosuid,nodev,mode=1777 $container_env"

case "$action" in
    init)
        slots=${2:-1}
        require_uint "$slots"
        if [ "$slots" -lt 1 ] || [ "$slots" -gt "$max_slots" ]; then exit 64; fi
        install -d -m 755 "$workspace/helpers"
        for name in newuidmap newgidmap; do
            install -m 4755 "/usr/bin/$name" "$workspace/helpers/$name"
        done
        ensure_slot_account 0
        migrate_existing_image_to_rootfs
        s=0
        while [ "$s" -lt "$slots" ]; do
            ensure_slot_account "$s"
            out=$(run_in_slot "$s" "info --format {{.Host.Security.Rootless}}" 1)
            [ "$out" = "true" ] || exit 1
            s=$((s + 1))
        done
        printf 'true\n'
        ;;
    verify)
        slots=${2:-1}
        require_uint "$slots"
        if [ "$slots" -lt 1 ] || [ "$slots" -gt "$max_slots" ]; then exit 64; fi
        s=0
        while [ "$s" -lt "$slots" ]; do
            [ -d "$(slot_home "$s")" ] || exit 1
            out=$(run_in_slot "$s" "info --format {{.Host.Security.Rootless}}" 1)
            [ "$out" = "true" ] || exit 1
            s=$((s + 1))
        done
        printf 'true\n'
        ;;
    check)
        slots=${2:-1}
        require_uint "$slots"
        if [ "$slots" -lt 1 ] || [ "$slots" -gt "$max_slots" ]; then exit 64; fi
        rootfs_present || exit 1
        version=""
        s=0
        while [ "$s" -lt "$slots" ]; do
            h=$(slot_home "$s")
            [ -d "$h/work" ] || exit 1
            version=$(run_in_slot "$s" "run $container_sec -v $h/home:/home/node -v $h/work:/workspace --rootfs $rootfs:O /usr/local/bin/codex --version" 1)
            [ "$version" = "codex-cli 0.167.0" ] || exit 1
            s=$((s + 1))
        done
        printf '%s\n' "$version"
        ;;
    load)
        rm -rf "$workspace/load-storage" "$workspace/rootfs.tmp" "$rootfs"
        mkdir -p "$workspace/rootfs.tmp"
        tar -xf - -C "$workspace/rootfs.tmp"
        normalize_rootfs_tree "$workspace/rootfs.tmp"
        mv "$workspace/rootfs.tmp" "$rootfs"
        rootfs_present || { echo "Loaded archive did not contain Codex rootfs" >&2; exit 125; }
        s=0
        while [ "$s" -lt "$max_slots" ]; do
            h=$(slot_home "$s")
            if [ -d "$h/home" ]; then
                seed_slot_home "$h" "$(slot_uid "$s")"
            fi
            s=$((s + 1))
        done
        ;;
    exec)
        slot=${2:-0}
        require_uint "$slot"
        if [ "$slot" -ge "$max_slots" ]; then exit 64; fi
        u=$(slot_user "$slot")
        h=$(slot_home "$slot")
        [ -d "$h/work" ] || { echo "Uninitialized Mini-pod slot $slot" >&2; exit 125; }
        rootfs_present || { echo "Missing Mini-pod Codex rootfs" >&2; exit 125; }
        podman_cmd="podman --root $h/overlay-storage --storage-driver overlay --cgroup-manager cgroupfs"
        pause_pid_file="$h/run/libpod/tmp/pause.pid"
        exec su -s /bin/sh "$u" -c "cd '$h'; export HOME='$h' XDG_CACHE_HOME='$h/cache' PATH='$workspace/helpers':\$PATH XDG_RUNTIME_DIR='$h/run'; if [ -f '$pause_pid_file' ] && ! kill -0 \"\$(cat '$pause_pid_file' 2>/dev/null)\" 2>/dev/null; then $podman_cmd system migrate >/dev/null 2>&1 || true; $podman_cmd container prune -f >/dev/null 2>&1 || true; fi; exec $podman_cmd run -i $container_sec -v '$h/home:/home/node' -v '$h/work:/workspace' --rootfs '$rootfs:O' /usr/local/bin/codex exec-server --listen stdio://"
        ;;
esac

# Shared helpers for the desktop packaging scripts.

fetch_runtime() {
    asset_dir=$1
    asset_name=$2
    asset_hash=$3
    asset_url=$4
    mkdir -p "$asset_dir"
    if [ ! -f "$asset_dir/$asset_name" ]; then
        curl -fsSL --retry 2 -o "$tmp/$asset_name.download" "$asset_url"
        printf '%s  %s\n' "$asset_hash" "$tmp/$asset_name.download" | shasum -a 256 -c
        mv "$tmp/$asset_name.download" "$asset_dir/$asset_name"
    fi
    (cd "$asset_dir" && printf '%s  %s\n' "$asset_hash" "$asset_name" | shasum -a 256 -c)
}

install_dependencies() {
    target=$1
    requirements=$2
    set -- --disable-pip-version-check --no-compile --no-deps --target "$target" -r "$requirements"
    if [ -n "${TG_WHEELHOUSE:-}" ]; then
        set -- "$@" --no-index --find-links "$TG_WHEELHOUSE"
    else
        set -- "$@" --index-url https://pypi.org/simple
    fi
    "${PYTHON:-python3}" -m pip --isolated install "$@"
}

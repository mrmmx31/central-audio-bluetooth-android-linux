#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 central-audio-bluetooth-android-linux contributors
set -uo pipefail
export LC_ALL=C

STATE_DIR="${XDG_STATE_HOME:-${HOME:?HOME is not set}/.local/state}/central-gtk2-audio"
ROUTE_STATE="$STATE_DIR/routes.tsv"
LEVEL_STATE="$STATE_DIR/levels.tsv"
ROUTE_HEADER='AUDIO_CENTER_ROUTES_V1'
LEVEL_HEADER='AUDIO_CENTER_LEVELS_V1'

error() { printf 'Erro: %s\n' "$*" >&2; return 1; }
pa() { timeout 8 pactl "$@"; }

usage() {
    cat <<'HELP'
Uso: audio-route.sh COMANDO [ARGUMENTOS]

Consultas (não gravam estado):
  status | inventory | tablet-source | scrcpy-status | obex-status

Perfis:
  tablet-profile          Usa a fonte tablet_mic / Microfone_Tablet.
  set-source NOME          Define a entrada padrão.
  set-sink NOME            Define a saída padrão.
  set-route ENTRADA SAIDA  Define os dois padrões.
  restore-routes          Recupera os padrões guardados.

Volumes e silenciamento:
  set-source-volume NOME PERCENTUAL
  set-sink-volume NOME PERCENTUAL
  set-sink-mute NOME 0|1
  set-sink-levels NOME PERCENTUAL 0|1
  restore-volumes         Recupera volumes por canal e silenciamento.
  restore-all             Tenta recuperar rotas e volumes.

As trocas de rota são recusadas quando há clientes de captura ativos.
O primeiro estado de cada categoria é preservado até a restauração.
Estado: ${XDG_STATE_HOME:-~/.local/state}/central-gtk2-audio/
HELP
}

need_audio() {
    command -v pactl >/dev/null 2>&1 || { error 'pactl não encontrado.'; return 1; }
    command -v timeout >/dev/null 2>&1 || { error 'timeout não encontrado.'; return 1; }
    pa info >/dev/null 2>&1 || { error 'servidor de áudio inacessível neste contexto.'; return 1; }
}

valid_name() {
    [[ -n "$1" && "$1" != -* && "$1" != *$'\t'* && "$1" != *$'\n'* && "$1" != *$'\r'* ]]
}

device_exists() {
    local kind="$1" name="$2" listing
    valid_name "$name" || return 1
    listing="$(pa list short "${kind}s")" || return 1
    awk -F '\t' -v target="$name" '$2 == target { found = 1 } END { exit !found }' <<<"$listing"
}

inventory_kind() {
    local kind="$1" listing
    listing="$(pa list "${kind}s")" || return 1
    awk -v kind="$kind" '
        function emit() {
            if (name != "") {
                monitor = (name ~ /\.monitor$/ ? 1 : 0)
                printf "%s\t%s\t%s\t%d\n", kind, name, description, monitor
            }
        }
        /^[^[:space:]]/ { emit(); name=""; description="" }
        /^[[:space:]]*Name: / { sub(/^[[:space:]]*Name: /, ""); name=$0 }
        /^[[:space:]]*Description: / { sub(/^[[:space:]]*Description: /, ""); description=$0 }
        END { emit() }
    ' <<<"$listing"
}

inventory() {
    need_audio || return 1
    inventory_kind source || return 1
    inventory_kind sink
}

tablet_source() {
    need_audio || return 1
    local listing candidate count
    listing="$(inventory_kind source)" || return 1
    for candidate in tablet_mic Microfone_Tablet; do
        if awk -F '\t' -v name="$candidate" '$2 == name && $4 == 0 { found=1 } END { exit !found }' <<<"$listing"; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    count="$(awk -F '\t' '$3 == "Microfone_Tablet" && $4 == 0 { n++ } END { print n+0 }' <<<"$listing")"
    if [[ "$count" == 1 ]]; then
        awk -F '\t' '$3 == "Microfone_Tablet" && $4 == 0 { print $2 }' <<<"$listing"
        return 0
    fi
    error 'a fonte do tablet não foi encontrada de forma única; nenhum monitor será escolhido automaticamente.'
}

route_guard() {
    local listing
    listing="$(pa list short source-outputs)" || { error 'não consegui verificar clientes de captura; rota preservada.'; return 1; }
    if awk -F '\t' 'NF >= 3 && $3 ~ /^[0-9]+$/ { found=1 } END { exit !found }' <<<"$listing"; then
        error 'há captura ou medidor de áudio ativo. Encerre a captura antes de trocar padrões de entrada/saída.'
        return 1
    fi
}

with_lock() {
    command -v flock >/dev/null 2>&1 || { error 'flock não encontrado; estado não alterado.'; return 1; }
    umask 077
    mkdir -p -- "$STATE_DIR" || { error 'não consegui criar o diretório de estado.'; return 1; }
    exec 9>"$STATE_DIR/lock" || { error 'não consegui abrir o bloqueio de estado.'; return 1; }
    flock -x 9 || { error 'não consegui bloquear o estado.'; return 1; }
    "$@"
}

complete_file() {
    local last_byte
    [[ -s "$1" && -f "$1" ]] || return 1
    last_byte="$(tail -c 1 -- "$1" | od -An -tu1)" || return 1
    [[ "$last_byte" =~ ^[[:space:]]*10[[:space:]]*$ ]]
}

validate_routes() {
    local -a lines
    complete_file "$ROUTE_STATE" || { error 'snapshot de rotas vazio ou truncado; arquivo preservado.'; return 1; }
    mapfile -t lines <"$ROUTE_STATE" || return 1
    [[ "${#lines[@]}" == 3 && "${lines[0]}" == "$ROUTE_HEADER" &&
       "${lines[1]}" == source$'\t'* && "${lines[2]}" == sink$'\t'* ]] ||
        { error 'snapshot de rotas inválido; arquivo preservado.'; return 1; }
    SAVED_SOURCE="${lines[1]#*$'\t'}"
    SAVED_SINK="${lines[2]#*$'\t'}"
    valid_name "$SAVED_SOURCE" && valid_name "$SAVED_SINK" ||
        { error 'nomes inválidos no snapshot; arquivo preservado.'; return 1; }
}

validate_levels() {
    local line kind name values mute extra key
    local -a lines
    local -A seen=()
    LEVEL_ROWS=()
    complete_file "$LEVEL_STATE" || { error 'snapshot de volumes vazio ou truncado; arquivo preservado.'; return 1; }
    mapfile -t lines <"$LEVEL_STATE" || return 1
    [[ "${#lines[@]}" -ge 2 && "${lines[0]}" == "$LEVEL_HEADER" ]] ||
        { error 'snapshot de volumes inválido; arquivo preservado.'; return 1; }
    for line in "${lines[@]:1}"; do
        IFS=$'\t' read -r kind name values mute extra <<<"$line"
        [[ "$kind" == source || "$kind" == sink ]] &&
            valid_name "$name" &&
            [[ "$values" =~ ^[0-9]+(,[0-9]+)*$ && "$mute" =~ ^[01]$ && -z "$extra" ]] ||
            { error 'registro inválido no snapshot de volumes; arquivo preservado.'; return 1; }
        key="$kind:$name"
        [[ -z "${seen[$key]+x}" ]] || { error 'registro duplicado no snapshot de volumes; arquivo preservado.'; return 1; }
        seen["$key"]=1
        LEVEL_ROWS+=("$line")
    done
}

save_routes() {
    if [[ -e "$ROUTE_STATE" ]]; then validate_routes; return $?; fi
    local source sink tmp
    source="$(pa get-default-source)" && sink="$(pa get-default-sink)" ||
        { error 'não consegui ler os padrões atuais; nada foi alterado.'; return 1; }
    valid_name "$source" && valid_name "$sink" ||
        { error 'o servidor não informou padrões válidos; nada foi alterado.'; return 1; }
    tmp="$(mktemp "$STATE_DIR/routes.XXXXXX")" || return 1
    if ! printf '%s\nsource\t%s\nsink\t%s\n' "$ROUTE_HEADER" "$source" "$sink" >"$tmp"; then
        rm -f -- "$tmp"; error 'não consegui gravar o snapshot; nada foi alterado.'; return 1
    fi
    if ! mv -f -- "$tmp" "$ROUTE_STATE"; then
        rm -f -- "$tmp"; error 'não consegui guardar o snapshot; nada foi alterado.'; return 1
    fi
}

channel_values() {
    local output values
    output="$(pa "get-$1-volume" "$2")" || return 1
    values="$(awk '
        /^Volume:/ {
            sub(/^Volume:[[:space:]]*/, "")
            n=split($0, channels, ",")
            for(i=1;i<=n;i++) {
                if(match(channels[i], /[0-9]+[[:space:]]*\/[[:space:]]*[0-9]+/)) {
                    value=substr(channels[i],RSTART,RLENGTH)
                    sub(/[[:space:]]*\/.*/,"",value)
                    gsub(/[[:space:]]/,"",value)
                    printf "%s%s", (count++ ? "," : ""), value
                }
            }
        }
        END { if(count) printf "\n" }
    ' <<<"$output")" || return 1
    [[ "$values" =~ ^[0-9]+(,[0-9]+)*$ ]] || return 1
    printf '%s\n' "$values"
}

mute_value() {
    local output
    output="$(pa "get-$1-mute" "$2")" || return 1
    case "$output" in
        'Mute: yes') printf '1\n' ;;
        'Mute: no') printf '0\n' ;;
        *) return 1 ;;
    esac
}

write_levels() {
    local tmp
    tmp="$(mktemp "$STATE_DIR/levels.XXXXXX")" || return 1
    if ! printf '%s\n' "$LEVEL_HEADER" >"$tmp" || ! printf '%s\n' "$@" >>"$tmp"; then
        rm -f -- "$tmp"; error 'falha ao gravar volumes; snapshot anterior preservado.'; return 1
    fi
    if ! mv -f -- "$tmp" "$LEVEL_STATE"; then
        rm -f -- "$tmp"; error 'falha ao guardar volumes; snapshot anterior preservado.'; return 1
    fi
}

save_level() {
    local kind="$1" name="$2" line saved_kind saved_name values mute
    LEVEL_ROWS=()
    if [[ -e "$LEVEL_STATE" ]]; then validate_levels || return 1; fi
    for line in "${LEVEL_ROWS[@]}"; do
        IFS=$'\t' read -r saved_kind saved_name _ <<<"$line"
        [[ "$kind" == "$saved_kind" && "$name" == "$saved_name" ]] && return 0
    done
    values="$(channel_values "$kind" "$name")" && mute="$(mute_value "$kind" "$name")" ||
        { error 'não consegui guardar volume e silenciamento originais; nada foi alterado.'; return 1; }
    LEVEL_ROWS+=("$kind"$'\t'"$name"$'\t'"$values"$'\t'"$mute")
    write_levels "${LEVEL_ROWS[@]}"
}

set_default() {
    local kind="$1" name="$2"
    need_audio && route_guard || return 1
    device_exists "$kind" "$name" || { error "dispositivo não encontrado: $name"; return 1; }
    save_routes || return 1
    pa "set-default-$kind" "$name" || { error 'o servidor recusou a troca; snapshot preservado.'; return 1; }
    printf 'Padrão de %s aplicado: %s. Perfil anterior guardado.\n' "$kind" "$name"
}

set_pair() {
    local source="$1" sink="$2" previous_source
    need_audio && route_guard || return 1
    device_exists source "$source" && device_exists sink "$sink" ||
        { error 'entrada ou saída não encontrada; nada foi alterado.'; return 1; }
    previous_source="$(pa get-default-source)" || return 1
    save_routes || return 1
    pa set-default-source "$source" || { error 'a entrada não foi aplicada; snapshot preservado.'; return 1; }
    if ! pa set-default-sink "$sink"; then
        if pa set-default-source "$previous_source"; then
            error 'a saída falhou; a entrada voltou ao valor anterior. Snapshot preservado.'
        else
            error 'a saída e a reversão da entrada falharam. Restauração parcial; snapshot preservado.'
        fi
        return 1
    fi
    printf 'Perfil aplicado: entrada %s; saída %s. Perfil anterior guardado.\n' "$source" "$sink"
}

set_volume() {
    local kind="$1" name="$2" percent="$3"
    [[ "$percent" =~ ^[0-9]{1,3}$ ]] || { error 'use um volume inteiro de 0 a 150.'; return 1; }
    percent=$((10#$percent))
    (( percent <= 150 )) || { error 'o volume máximo aceito é 150%.'; return 1; }
    need_audio || return 1
    device_exists "$kind" "$name" || { error "dispositivo não encontrado: $name"; return 1; }
    save_level "$kind" "$name" || return 1
    pa "set-$kind-volume" "$name" "${percent}%" ||
        { error 'o volume não foi aplicado; estado anterior preservado.'; return 1; }
    printf 'Volume aplicado: %s%%. Volume original por canal e silenciamento guardados.\n' "$percent"
}

set_mute() {
    local name="$1" mute="$2"
    [[ "$mute" =~ ^[01]$ ]] || { error 'use 0 ou 1 para silenciamento.'; return 1; }
    need_audio || return 1
    device_exists sink "$name" || { error "saída não encontrada: $name"; return 1; }
    save_level sink "$name" || return 1
    pa set-sink-mute "$name" "$mute" || { error 'silenciamento não aplicado; estado anterior preservado.'; return 1; }
    printf 'Silenciamento aplicado. Estado anterior guardado.\n'
}

set_sink_levels() {
    local name="$1" percent="$2" mute="$3"
    [[ "$mute" =~ ^[01]$ ]] || { error 'use 0 ou 1 para silenciamento.'; return 1; }
    set_volume sink "$name" "$percent" || return 1
    pa set-sink-mute "$name" "$mute" ||
        { error 'volume aplicado, mas o silenciamento falhou; estado original preservado.'; return 1; }
    printf 'Silenciamento aplicado.\n'
}

restore_routes() {
    [[ -e "$ROUTE_STATE" ]] || { printf 'Nenhuma rota guardada.\n'; return 0; }
    validate_routes && need_audio && route_guard || return 1
    device_exists source "$SAVED_SOURCE" && device_exists sink "$SAVED_SINK" ||
        { error 'um dispositivo original não está disponível; snapshot preservado.'; return 1; }
    local failed=0
    if ! pa set-default-source "$SAVED_SOURCE"; then failed=1; error 'falha ao restaurar a entrada; snapshot preservado.'; fi
    if ! pa set-default-sink "$SAVED_SINK"; then failed=1; error 'falha ao restaurar a saída; snapshot preservado.'; fi
    (( failed == 0 )) || return 1
    rm -f -- "$ROUTE_STATE" || { error 'rotas aplicadas, mas o snapshot não pôde ser removido.'; return 1; }
    printf 'Entrada e saída padrão restauradas.\n'
}

restore_levels() {
    [[ -e "$LEVEL_STATE" ]] || { printf 'Nenhum volume guardado.\n'; return 0; }
    validate_levels && need_audio || return 1
    local line kind name values mute row_failed failed=0
    local -a remaining=() channels=()
    for line in "${LEVEL_ROWS[@]}"; do
        IFS=$'\t' read -r kind name values mute <<<"$line"
        row_failed=0
        if device_exists "$kind" "$name"; then
            IFS=',' read -r -a channels <<<"$values"
            if ! pa "set-$kind-volume" "$name" "${channels[@]}"; then row_failed=1; fi
            if ! pa "set-$kind-mute" "$name" "$mute"; then row_failed=1; fi
        else
            row_failed=1
        fi
        if (( row_failed )); then
            remaining+=("$line")
            failed=1
            error "volume/silenciamento ainda pendente: $name"
        else
            printf 'Volume por canal e silenciamento restaurados: %s.\n' "$name"
        fi
    done
    if (( failed )); then
        write_levels "${remaining[@]}" || return 1
        return 1
    fi
    rm -f -- "$LEVEL_STATE" || { error 'volumes aplicados, mas o snapshot não pôde ser removido.'; return 1; }
}

restore_all() {
    local failed=0
    restore_routes || failed=1
    restore_levels || failed=1
    return "$failed"
}

status() {
    need_audio || return 1
    local source sink
    source="$(pa get-default-source)" && sink="$(pa get-default-sink)" || return 1
    printf 'Entrada padrão: %s\nSaída padrão: %s\n' "$source" "$sink"
    [[ "$source" != *.monitor ]] || printf 'Atenção: a entrada padrão é um monitor da saída, não um microfone físico.\n'
    inventory_kind source && inventory_kind sink
}

scrcpy_status() {
    printf 'Verificação somente de leitura.\n'
    if command -v scrcpy >/dev/null 2>&1; then
        printf 'scrcpy no PATH: disponível.\n'
    else
        printf 'scrcpy no PATH: não localizado; pode estar fora do PATH.\n'
    fi
    if command -v pgrep >/dev/null 2>&1 && pgrep -x scrcpy >/dev/null 2>&1; then
        printf 'Processo scrcpy: detectado.\n'
    else
        printf 'Processo scrcpy: não detectado neste contexto.\n'
    fi
    if need_audio >/dev/null 2>&1; then
        local source
        if source="$(tablet_source 2>/dev/null)"; then
            printf 'Fonte do tablet: disponível (%s).\n' "$source"
        else
            printf 'Fonte do tablet: não localizada.\n'
        fi
    else
        printf 'Fonte do tablet: consulta indisponível.\n'
    fi
    printf 'Estes estados não confirmam chegada de voz nem seleção na chamada.\n'
}

obex_status() {
    printf 'Verificação local de OBEX.\n'
    if command -v obexctl >/dev/null 2>&1; then
        obexctl --version || true
    else
        printf 'obexctl: não encontrado.\n'
    fi
    local path candidate bluetooth_state obex_state
    path="$(command -v obexd 2>/dev/null || true)"
    if [[ -z "$path" ]]; then
        for candidate in /usr/libexec/bluetooth/obexd /usr/lib/bluetooth/obexd; do
            if [[ -x "$candidate" ]]; then path="$candidate"; break; fi
        done
    fi
    if [[ -n "$path" ]]; then printf 'obexd: disponível.\n'; else printf 'obexd: não localizado.\n'; fi
    if command -v systemctl >/dev/null 2>&1; then
        bluetooth_state="$(timeout 3 systemctl is-active bluetooth.service 2>&1)" || true
        obex_state="$(timeout 3 systemctl --user is-active obex.service 2>&1)" || true
        [[ "$bluetooth_state" != *'Operation not permitted'* ]] || bluetooth_state='consulta bloqueada'
        [[ "$obex_state" != *'Operation not permitted'* ]] || obex_state='consulta bloqueada'
        printf 'Bluetooth: %s\nOBEX do usuário: %s\n' "$bluetooth_state" "$obex_state"
    fi
    printf 'Não confirma suporte do Android. Nenhum pareamento ou envio foi iniciado.\n'
}

main() {
    local command_name="${1:-help}" source
    case "$command_name" in
        help|-h|--help) usage ;;
        status) status ;;
        inventory) inventory ;;
        tablet-source) tablet_source ;;
        scrcpy-status) scrcpy_status ;;
        obex-status) obex_status ;;
        tablet-profile)
            source="$(tablet_source)" || return 1
            with_lock set_default source "$source" ;;
        set-source|set-sink)
            [[ $# == 2 ]] || { error "uso: $command_name NOME"; return 1; }
            with_lock set_default "${command_name#set-}" "$2" ;;
        set-route)
            [[ $# == 3 ]] || { error 'uso: set-route ENTRADA SAIDA'; return 1; }
            with_lock set_pair "$2" "$3" ;;
        set-source-volume|set-sink-volume)
            [[ $# == 3 ]] || { error "uso: $command_name NOME PERCENTUAL"; return 1; }
            local kind="${command_name#set-}"; kind="${kind%-volume}"
            with_lock set_volume "$kind" "$2" "$3" ;;
        set-sink-mute)
            [[ $# == 3 ]] || { error 'uso: set-sink-mute NOME 0|1'; return 1; }
            with_lock set_mute "$2" "$3" ;;
        set-sink-levels)
            [[ $# == 4 ]] || { error 'uso: set-sink-levels NOME PERCENTUAL 0|1'; return 1; }
            with_lock set_sink_levels "$2" "$3" "$4" ;;
        restore-routes) with_lock restore_routes ;;
        restore-volumes) with_lock restore_levels ;;
        restore-all) with_lock restore_all ;;
        *) usage >&2; error "comando desconhecido: $command_name" ;;
    esac
}
main "$@"

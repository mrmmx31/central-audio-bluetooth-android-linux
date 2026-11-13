/* SPDX-License-Identifier: GPL-2.0-only
 * Copyright (C) 2026 central-audio-bluetooth-android-linux contributors
 */
#include <gtk/gtk.h>
#include <stdlib.h>
#include <string.h>

#define APP_VERSION "0.1.0"

typedef struct {
    gchar *name;
    gchar *label;
    gboolean monitor;
} Device;

typedef struct {
    GtkWidget *window, *status, *echo, *tablet_radio, *local_radio, *local_combo;
    GtkWidget *sink_combo, *source_scale, *sink_scale, *mute;
    GtkWidget *apply_source, *apply_sink, *apply_gain, *apply_levels;
    GtkWidget *scrcpy_state, *obex_state, *restore;
    GtkWidget *serial, *scrcpy_path, *connect, *disconnect, *reconnect;
    GPtrArray *sources, *sinks;
    gchar *helper, *tablet;
    gboolean updating, audio_ok, capture_active;
} App;

static gboolean command(gchar **argv, gchar **output, gchar **problem) {
    GError *error = NULL;
    gint status = 0;
    gchar *out = NULL, *err = NULL;
    gchar **env = g_get_environ();
    env = g_environ_setenv(env, "LC_ALL", "C", TRUE);
    gboolean launched = g_spawn_sync(NULL, argv, env, G_SPAWN_SEARCH_PATH,
        NULL, NULL, &out, &err, &status, &error);
    g_strfreev(env);
    if (!launched || !g_spawn_check_wait_status(status, launched ? &error : NULL)) {
        if (problem) {
            *problem = g_strdup_printf("%s%s%s%s",
                out && *out ? out : "", out && *out && err && *err ? "\n" : "",
                err && *err ? err : "",
                (!out || !*out) && (!err || !*err) && error ? error->message : "");
            g_strstrip(*problem);
        }
        g_clear_error(&error);
        g_free(out);
        g_free(err);
        return FALSE;
    }
    if (output) *output = out; else g_free(out);
    g_free(err);
    return TRUE;
}

static gboolean helper(App *app, const gchar *action, const gchar *a,
                       const gchar *b, const gchar *c, gchar **output, gchar **problem) {
    gchar *argv[] = {app->helper, (gchar *)action, (gchar *)a,
        (gchar *)b, (gchar *)c, NULL};
    return command(argv, output, problem);
}

static gboolean session_command(App *app, const gchar *action, gchar **out, gchar **problem) {
    gchar *directory = g_path_get_dirname(app->helper);
    gchar *path = g_build_filename(directory, "tablet-session.py", NULL);
    const gchar *serial = gtk_entry_get_text(GTK_ENTRY(app->serial));
    const gchar *scrcpy = gtk_entry_get_text(GTK_ENTRY(app->scrcpy_path));
    gchar *argv[9] = {(gchar *)"python3", path, (gchar *)action, NULL};
    guint n = 3;
    if (*serial && (!strcmp(action, "start") || !strcmp(action, "reconnect"))) {
        argv[n++] = (gchar *)"--serial"; argv[n++] = (gchar *)serial;
    }
    if (*scrcpy && strcmp(action, "status") && strcmp(action, "stop")) {
        argv[n++] = (gchar *)"--scrcpy"; argv[n++] = (gchar *)scrcpy;
    }
    argv[n] = NULL;
    gboolean ok = command(argv, out, problem);
    g_free(path);
    g_free(directory);
    return ok;
}

static gchar *pactl_value(const gchar *action, const gchar *name) {
    gchar *argv[] = {(gchar *)"timeout", (gchar *)"8", (gchar *)"pactl",
        (gchar *)action, (gchar *)name, NULL};
    gchar *out = NULL;
    if (!command(argv, &out, NULL)) return NULL;
    g_strstrip(out);
    return out;
}

static void message(App *app, GtkMessageType type, const gchar *title, const gchar *text) {
    GtkWidget *dialog = gtk_message_dialog_new(GTK_WINDOW(app->window),
        GTK_DIALOG_MODAL | GTK_DIALOG_DESTROY_WITH_PARENT, type, GTK_BUTTONS_CLOSE,
        "%s", title);
    gtk_message_dialog_format_secondary_text(GTK_MESSAGE_DIALOG(dialog), "%s", text ? text : "");
    gtk_dialog_run(GTK_DIALOG(dialog));
    gtk_widget_destroy(dialog);
}

static void device_free(gpointer data) {
    Device *device = data;
    g_free(device->name);
    g_free(device->label);
    g_free(device);
}

static GtkWidget *device_combo(void) {
    GtkListStore *store = gtk_list_store_new(2, G_TYPE_STRING, G_TYPE_STRING);
    GtkWidget *combo = gtk_combo_box_new_with_model(GTK_TREE_MODEL(store));
    GtkCellRenderer *renderer = gtk_cell_renderer_text_new();
    gtk_cell_layout_pack_start(GTK_CELL_LAYOUT(combo), renderer, TRUE);
    gtk_cell_layout_set_attributes(GTK_CELL_LAYOUT(combo), renderer, "text", 1, NULL);
    g_object_unref(store);
    return combo;
}

static void combo_add(GtkWidget *combo, Device *device) {
    GtkListStore *store = GTK_LIST_STORE(gtk_combo_box_get_model(GTK_COMBO_BOX(combo)));
    GtkTreeIter iter;
    gtk_list_store_append(store, &iter);
    gtk_list_store_set(store, &iter, 0, device->name, 1, device->label, -1);
}

static gchar *combo_name(GtkWidget *combo) {
    GtkTreeIter iter;
    gchar *name = NULL;
    if (gtk_combo_box_get_active_iter(GTK_COMBO_BOX(combo), &iter))
        gtk_tree_model_get(gtk_combo_box_get_model(GTK_COMBO_BOX(combo)), &iter, 0, &name, -1);
    return name;
}

static gboolean combo_select(GtkWidget *combo, const gchar *name) {
    GtkTreeModel *model = gtk_combo_box_get_model(GTK_COMBO_BOX(combo));
    GtkTreeIter iter;
    gboolean valid = gtk_tree_model_get_iter_first(model, &iter);
    while (valid) {
        gchar *candidate = NULL;
        gtk_tree_model_get(model, &iter, 0, &candidate, -1);
        gboolean match = name && candidate && !strcmp(name, candidate);
        g_free(candidate);
        if (match) {
            gtk_combo_box_set_active_iter(GTK_COMBO_BOX(combo), &iter);
            return TRUE;
        }
        valid = gtk_tree_model_iter_next(model, &iter);
    }
    return FALSE;
}

static const gchar *device_label(GPtrArray *devices, const gchar *name) {
    for (guint i = 0; i < devices->len; i++) {
        Device *device = g_ptr_array_index(devices, i);
        if (name && !strcmp(device->name, name)) return device->label;
    }
    return name ? name : "indisponível";
}

static gchar *selected_source(App *app) {
    if (gtk_toggle_button_get_active(GTK_TOGGLE_BUTTON(app->tablet_radio)))
        return g_strdup(app->tablet);
    return combo_name(app->local_combo);
}

static gint percent(const gchar *text) {
    GRegex *regex = g_regex_new("([0-9]+)%", 0, 0, NULL);
    GMatchInfo *match = NULL;
    gint value = -1;
    if (g_regex_match(regex, text ? text : "", 0, &match)) {
        gchar *number = g_match_info_fetch(match, 1);
        value = atoi(number);
        g_free(number);
    }
    if (match) g_match_info_free(match);
    g_regex_unref(regex);
    return value;
}

static void read_volume(GtkWidget *scale, const gchar *kind, const gchar *name) {
    if (!name) return;
    gchar *action = g_strdup_printf("get-%s-volume", kind);
    gchar *out = pactl_value(action, name);
    gint value = percent(out);
    if (value >= 0) {
        gtk_range_set_range(GTK_RANGE(scale), 0, MAX(150, value));
        gtk_range_set_value(GTK_RANGE(scale), value);
    }
    g_free(action);
    g_free(out);
}

static void input_changed(GtkWidget *widget, gpointer data) {
    App *app = data;
    if (app->updating) return;
    gboolean local = gtk_toggle_button_get_active(GTK_TOGGLE_BUTTON(app->local_radio));
    gtk_widget_set_sensitive(app->local_combo, local && app->audio_ok);
    gchar *name = selected_source(app);
    read_volume(app->source_scale, "source", name);
    gtk_widget_set_sensitive(app->apply_gain, app->audio_ok && name);
    gtk_widget_set_sensitive(app->apply_source, app->audio_ok && !app->capture_active && name);
    g_free(name);
    (void)widget;
}

static void output_changed(GtkWidget *widget, gpointer data) {
    App *app = data;
    if (app->updating) return;
    gchar *name = combo_name(app->sink_combo);
    read_volume(app->sink_scale, "sink", name);
    gchar *out = name ? pactl_value("get-sink-mute", name) : NULL;
    app->updating = TRUE;
    gtk_toggle_button_set_active(GTK_TOGGLE_BUTTON(app->mute), out && !strcmp(out, "Mute: yes"));
    app->updating = FALSE;
    gtk_widget_set_sensitive(app->apply_sink, app->audio_ok && !app->capture_active && name);
    gtk_widget_set_sensitive(app->apply_levels, app->audio_ok && name);
    g_free(name);
    g_free(out);
    (void)widget;
}

static gboolean active_capture(void) {
    gchar *argv[] = {(gchar *)"timeout", (gchar *)"8", (gchar *)"pactl",
        (gchar *)"list", (gchar *)"short", (gchar *)"source-outputs", NULL};
    gchar *out = NULL;
    if (!command(argv, &out, NULL)) return TRUE;
    gboolean found = FALSE;
    gchar **lines = g_strsplit(out, "\n", -1);
    for (guint i = 0; lines[i]; i++) {
        gchar **fields = g_strsplit(lines[i], "\t", -1);
        if (g_strv_length(fields) >= 3 && *fields[2]) {
            const gchar *p = fields[2];
            while (g_ascii_isdigit(*p)) p++;
            if (!*p) found = TRUE;
        }
        g_strfreev(fields);
    }
    g_strfreev(lines);
    g_free(out);
    return found;
}

static void scrcpy_query(App *app) {
    gchar *out = NULL, *problem = NULL;
    if (session_command(app, "status", &out, &problem)) {
        gchar *text = g_strdup_printf("%s\n%s", out,
            app->tablet ? "Fonte do tablet disponível; voz ainda requer teste." : "Fonte do tablet não detectada neste contexto.");
        gtk_label_set_text(GTK_LABEL(app->scrcpy_state), text);
        g_free(text);
    } else gtk_label_set_text(GTK_LABEL(app->scrcpy_state), problem ? problem : "Estado indisponível.");
    g_free(out);
    g_free(problem);
}

static void refresh(App *app) {
    gchar *out = NULL, *problem = NULL;
    gchar *old_local = combo_name(app->local_combo);
    gchar *old_sink = combo_name(app->sink_combo);
    app->updating = TRUE;
    app->audio_ok = helper(app, "inventory", NULL, NULL, NULL, &out, &problem);
    if (app->audio_ok) {
        g_ptr_array_set_size(app->sources, 0);
        g_ptr_array_set_size(app->sinks, 0);
        g_free(app->tablet);
        app->tablet = NULL;
        gtk_list_store_clear(GTK_LIST_STORE(gtk_combo_box_get_model(GTK_COMBO_BOX(app->local_combo))));
        gtk_list_store_clear(GTK_LIST_STORE(gtk_combo_box_get_model(GTK_COMBO_BOX(app->sink_combo))));
        gchar **lines = g_strsplit(out, "\n", -1);
        for (guint i = 0; lines[i]; i++) {
            gchar **fields = g_strsplit(lines[i], "\t", -1);
            if (g_strv_length(fields) == 4 && *fields[1]) {
                Device *device = g_new0(Device, 1);
                device->name = g_strdup(fields[1]);
                device->label = g_strdup(*fields[2] ? fields[2] : fields[1]);
                device->monitor = !strcmp(fields[3], "1");
                if (!strcmp(fields[0], "source")) g_ptr_array_add(app->sources, device);
                else if (!strcmp(fields[0], "sink")) g_ptr_array_add(app->sinks, device);
                else device_free(device);
            }
            g_strfreev(fields);
        }
        g_strfreev(lines);
        for (guint i = 0; i < app->sources->len; i++) {
            Device *device = g_ptr_array_index(app->sources, i);
            if (!device->monitor && (!strcmp(device->name, "tablet_mic") ||
                !strcmp(device->name, "Microfone_Tablet"))) {
                app->tablet = g_strdup(device->name);
                break;
            }
        }
        if (!app->tablet) {
            guint matches = 0;
            Device *candidate = NULL;
            for (guint i = 0; i < app->sources->len; i++) {
                Device *device = g_ptr_array_index(app->sources, i);
                if (!device->monitor && !strcmp(device->label, "Microfone_Tablet")) {
                    matches++;
                    candidate = device;
                }
            }
            if (matches == 1) app->tablet = g_strdup(candidate->name);
        }
        for (guint i = 0; i < app->sources->len; i++) {
            Device *device = g_ptr_array_index(app->sources, i);
            if (!device->monitor && g_strcmp0(device->name, app->tablet)) combo_add(app->local_combo, device);
        }
        for (guint i = 0; i < app->sinks->len; i++) {
            Device *device = g_ptr_array_index(app->sinks, i);
            if (strcmp(device->name, "tablet_mic_bus")) combo_add(app->sink_combo, device);
        }
        gtk_combo_box_set_active(GTK_COMBO_BOX(app->local_combo), 0);
        gtk_combo_box_set_active(GTK_COMBO_BOX(app->sink_combo), 0);
        if (old_local) combo_select(app->local_combo, old_local);
        if (old_sink) combo_select(app->sink_combo, old_sink);
        gchar *default_source = pactl_value("get-default-source", NULL);
        gchar *default_sink = pactl_value("get-default-sink", NULL);
        if (default_sink) combo_select(app->sink_combo, default_sink);
        if (default_source && app->tablet && !strcmp(default_source, app->tablet))
            gtk_toggle_button_set_active(GTK_TOGGLE_BUTTON(app->tablet_radio), TRUE);
        else if (default_source && combo_select(app->local_combo, default_source))
            gtk_toggle_button_set_active(GTK_TOGGLE_BUTTON(app->local_radio), TRUE);
        else if (app->tablet)
            gtk_toggle_button_set_active(GTK_TOGGLE_BUTTON(app->tablet_radio), TRUE);
        app->capture_active = active_capture();
        gchar *status = g_strdup_printf("Padrões: entrada %s · saída %s.\n%s",
            device_label(app->sources, default_source), device_label(app->sinks, default_sink),
            app->capture_active ? "Captura ou medidor ativo: trocas de perfil bloqueadas." :
                                  "Escolha uma origem e aplique o perfil quando estiver pronto.");
        gtk_label_set_text(GTK_LABEL(app->status), status);
        if (default_source && g_str_has_suffix(default_source, ".monitor"))
            gtk_label_set_text(GTK_LABEL(app->echo),
                "Ao usar o tablet, prefira fones ou reduza os alto-falantes.\nA entrada padrão é um monitor de saída; revise antes de iniciar outra chamada.");
        else
            gtk_label_set_text(GTK_LABEL(app->echo),
                "Ao usar o microfone do tablet, prefira fones ou reduza o volume dos alto-falantes.");
        g_free(status);
        g_free(default_source);
        g_free(default_sink);
    } else {
        app->capture_active = TRUE;
        gtk_label_set_text(GTK_LABEL(app->status), "Servidor de áudio indisponível neste contexto. Controles de áudio desativados.");
        gtk_widget_set_tooltip_text(app->status, problem);
    }
    gchar *local = combo_name(app->local_combo);
    gtk_widget_set_sensitive(app->tablet_radio, app->audio_ok && app->tablet);
    gtk_widget_set_sensitive(app->local_radio, app->audio_ok && local);
    gtk_widget_set_sensitive(app->sink_combo, app->audio_ok);
    gtk_widget_set_sensitive(app->source_scale, app->audio_ok);
    gtk_widget_set_sensitive(app->sink_scale, app->audio_ok);
    gtk_widget_set_sensitive(app->mute, app->audio_ok);
    gtk_widget_set_sensitive(app->restore, app->audio_ok);
    gtk_widget_set_sensitive(app->connect, app->audio_ok && !app->capture_active);
    gtk_widget_set_sensitive(app->disconnect, app->audio_ok && !app->capture_active);
    gtk_widget_set_sensitive(app->reconnect, app->audio_ok && !app->capture_active);
    app->updating = FALSE;
    input_changed(NULL, app);
    output_changed(NULL, app);
    scrcpy_query(app);
    g_free(local);
    g_free(old_local);
    g_free(old_sink);
    g_free(out);
    g_free(problem);
}

static void act(App *app, const gchar *action, const gchar *a, const gchar *b,
                const gchar *c, const gchar *title) {
    gchar *out = NULL, *problem = NULL;
    if (helper(app, action, a, b, c, &out, &problem))
        message(app, GTK_MESSAGE_INFO, title, out);
    else
        message(app, GTK_MESSAGE_WARNING, "Ação não concluída", problem);
    g_free(out);
    g_free(problem);
    refresh(app);
}

static void apply_input(GtkButton *button, gpointer data) {
    App *app = data;
    gchar *name = selected_source(app);
    if (name) act(app, "set-source", name, NULL, NULL, "Entrada padrão aplicada");
    g_free(name);
    (void)button;
}

static void apply_output(GtkButton *button, gpointer data) {
    App *app = data;
    gchar *name = combo_name(app->sink_combo);
    if (name) act(app, "set-sink", name, NULL, NULL, "Saída padrão aplicada");
    g_free(name);
    (void)button;
}

static void apply_gain(GtkButton *button, gpointer data) {
    App *app = data;
    gchar *name = selected_source(app);
    gchar *value = g_strdup_printf("%d", (gint)gtk_range_get_value(GTK_RANGE(app->source_scale)));
    if (name) act(app, "set-source-volume", name, value, NULL, "Ganho aplicado");
    g_free(name);
    g_free(value);
    (void)button;
}

static void apply_levels(GtkButton *button, gpointer data) {
    App *app = data;
    gchar *name = combo_name(app->sink_combo);
    gchar *value = g_strdup_printf("%d", (gint)gtk_range_get_value(GTK_RANGE(app->sink_scale)));
    const gchar *mute = gtk_toggle_button_get_active(GTK_TOGGLE_BUTTON(app->mute)) ? "1" : "0";
    if (name) act(app, "set-sink-levels", name, value, mute, "Volume e silenciamento aplicados");
    g_free(name);
    g_free(value);
    (void)button;
}

static void restore_clicked(GtkButton *button, gpointer data) {
    act(data, "restore-all", NULL, NULL, NULL, "Perfil restaurado");
    (void)button;
}

static void refresh_clicked(GtkButton *button, gpointer data) {
    refresh(data);
    (void)button;
}

static void session_action(App *app, const gchar *action) {
    gchar *out = NULL, *problem = NULL;
    if (session_command(app, action, &out, &problem))
        message(app, GTK_MESSAGE_INFO, "Conexão com o tablet", out);
    else message(app, GTK_MESSAGE_WARNING, "Conexão não concluída", problem);
    g_free(out);
    g_free(problem);
    refresh(app);
}

static void connect_clicked(GtkButton *button, gpointer data) { session_action(data, "start"); (void)button; }
static void disconnect_clicked(GtkButton *button, gpointer data) { session_action(data, "stop"); (void)button; }
static void reconnect_clicked(GtkButton *button, gpointer data) { session_action(data, "reconnect"); (void)button; }
static void dependencies_clicked(GtkButton *button, gpointer data) { session_action(data, "check"); (void)button; }

static void obex_clicked(GtkButton *button, gpointer data) {
    App *app = data;
    gchar *out = NULL, *problem = NULL;
    if (helper(app, "obex-status", NULL, NULL, NULL, &out, &problem)) {
        gtk_label_set_text(GTK_LABEL(app->obex_state), "Consulta local concluída. Suporte do Android ainda precisa de teste.");
        message(app, GTK_MESSAGE_INFO, "Bluetooth · transferência de arquivos", out);
    } else message(app, GTK_MESSAGE_WARNING, "Consulta indisponível", problem);
    g_free(out);
    g_free(problem);
    (void)button;
}

static GtkWidget *label(const gchar *text, gboolean wrap) {
    GtkWidget *widget = gtk_label_new(text);
    gtk_label_set_line_wrap(GTK_LABEL(widget), wrap);
    gtk_misc_set_alignment(GTK_MISC(widget), 0, 0.5);
    return widget;
}

static GtkWidget *heading(const gchar *text, gint size) {
    GtkWidget *widget = label(text, FALSE);
    PangoFontDescription *font = pango_font_description_new();
    pango_font_description_set_weight(font, PANGO_WEIGHT_BOLD);
    pango_font_description_set_size(font, size * PANGO_SCALE);
    gtk_widget_modify_font(widget, font);
    pango_font_description_free(font);
    return widget;
}

static GtkWidget *panel(GtkWidget *child, const gchar *color) {
    GtkWidget *frame = gtk_frame_new(NULL);
    gtk_frame_set_shadow_type(GTK_FRAME(frame), GTK_SHADOW_ETCHED_IN);
    GtkWidget *background = gtk_event_box_new();
    GdkColor bg;
    gdk_color_parse(color, &bg);
    gtk_widget_modify_bg(background, GTK_STATE_NORMAL, &bg);
    gtk_container_add(GTK_CONTAINER(frame), background);
    GtkWidget *padding = gtk_alignment_new(0, 0, 1, 1);
    gtk_alignment_set_padding(GTK_ALIGNMENT(padding), 14, 14, 18, 18);
    gtk_container_add(GTK_CONTAINER(background), padding);
    gtk_container_add(GTK_CONTAINER(padding), child);
    return frame;
}

static GtkWidget *action_button(const gchar *text, GCallback callback, App *app) {
    GtkWidget *button = gtk_button_new_with_label(text);
    g_signal_connect(button, "clicked", callback, app);
    return button;
}

static GtkWidget *scale(void) {
    GtkWidget *widget = gtk_hscale_new_with_range(0, 150, 1);
    gtk_scale_set_digits(GTK_SCALE(widget), 0);
    gtk_scale_set_value_pos(GTK_SCALE(widget), GTK_POS_RIGHT);
    gtk_range_set_value(GTK_RANGE(widget), 100);
    gtk_widget_set_tooltip_text(widget, "Percentual do dispositivo. Acima de 100% pode distorcer. Só muda ao clicar em Aplicar.");
    return widget;
}

static gchar *helper_path(const gchar *argv0) {
    gchar *path = g_find_program_in_path(argv0);
    if (!path) path = g_canonicalize_filename(argv0, NULL);
    gchar *directory = g_path_get_dirname(path);
    gchar *result = g_build_filename(directory, "audio-route.sh", NULL);
    g_free(directory);
    g_free(path);
    return result;
}

int main(int argc, char **argv) {
    if (argc == 2 && !strcmp(argv[1], "--version")) {
        g_print("central-audio %s\n", APP_VERSION);
        return 0;
    }
    gtk_init(&argc, &argv);
    App *app = g_new0(App, 1);
    app->helper = helper_path(argv[0]);
    app->sources = g_ptr_array_new_with_free_func(device_free);
    app->sinks = g_ptr_array_new_with_free_func(device_free);
    app->updating = TRUE;
    app->window = gtk_window_new(GTK_WINDOW_TOPLEVEL);
    gtk_window_set_title(GTK_WINDOW(app->window), "Central de áudio");
    gtk_window_set_default_size(GTK_WINDOW(app->window), 940, 900);
    g_signal_connect(app->window, "destroy", G_CALLBACK(gtk_main_quit), NULL);
    GdkColor body;
    gdk_color_parse("#f0f0ed", &body);
    gtk_widget_modify_bg(app->window, GTK_STATE_NORMAL, &body);
    GtkWidget *scroll = gtk_scrolled_window_new(NULL, NULL);
    gtk_scrolled_window_set_policy(GTK_SCROLLED_WINDOW(scroll), GTK_POLICY_NEVER, GTK_POLICY_AUTOMATIC);
    gtk_container_add(GTK_CONTAINER(app->window), scroll);
    GtkWidget *page = gtk_vbox_new(FALSE, 16);
    gtk_container_set_border_width(GTK_CONTAINER(page), 24);
    gtk_scrolled_window_add_with_viewport(GTK_SCROLLED_WINDOW(scroll), page);
    gtk_widget_modify_bg(gtk_bin_get_child(GTK_BIN(scroll)), GTK_STATE_NORMAL, &body);

    GtkWidget *header = gtk_hbox_new(FALSE, 14);
    GtkWidget *intro = gtk_vbox_new(FALSE, 6);
    gtk_box_pack_start(GTK_BOX(intro), heading("Áudio do tablet e notebook", 18), FALSE, FALSE, 0);
    app->status = label("Consultando o servidor de áudio…", TRUE);
    gtk_box_pack_start(GTK_BOX(intro), app->status, FALSE, FALSE, 0);
    gtk_box_pack_start(GTK_BOX(header), intro, TRUE, TRUE, 0);
    GtkWidget *refresh_button = action_button("Atualizar estado", G_CALLBACK(refresh_clicked), app);
    GtkWidget *refresh_alignment = gtk_alignment_new(1, 0, 0, 0);
    gtk_widget_set_size_request(refresh_button, 150, 34);
    gtk_container_add(GTK_CONTAINER(refresh_alignment), refresh_button);
    gtk_box_pack_start(GTK_BOX(header), refresh_alignment, FALSE, FALSE, 0);
    gtk_box_pack_start(GTK_BOX(page), header, FALSE, FALSE, 0);

    GtkWidget *warning = gtk_vbox_new(FALSE, 6);
    gtk_box_pack_start(GTK_BOX(warning), heading("! Atenção ao eco", 11), FALSE, FALSE, 0);
    app->echo = label("Ao usar o microfone do tablet, prefira fones ou reduza o volume dos alto-falantes.", TRUE);
    gtk_label_set_width_chars(GTK_LABEL(app->echo), 100);
    gtk_box_pack_start(GTK_BOX(warning), app->echo, FALSE, FALSE, 0);
    gtk_box_pack_start(GTK_BOX(page), panel(warning, "#fff4d7"), FALSE, FALSE, 0);

    GtkWidget *columns = gtk_hbox_new(TRUE, 16);
    GtkWidget *mic = gtk_vbox_new(FALSE, 9);
    gtk_box_pack_start(GTK_BOX(mic), heading("Microfone", 11), FALSE, FALSE, 0);
    gtk_box_pack_start(GTK_BOX(mic), label("Escolha a origem para aplicar", FALSE), FALSE, FALSE, 0);
    app->tablet_radio = gtk_radio_button_new_with_label(NULL, "Tablet · Microfone_Tablet");
    app->local_radio = gtk_radio_button_new_with_label_from_widget(
        GTK_RADIO_BUTTON(app->tablet_radio), "Notebook · entrada local selecionada");
    app->local_combo = device_combo();
    gtk_box_pack_start(GTK_BOX(mic), app->tablet_radio, FALSE, FALSE, 0);
    gtk_box_pack_start(GTK_BOX(mic), app->local_radio, FALSE, FALSE, 0);
    gtk_box_pack_start(GTK_BOX(mic), app->local_combo, FALSE, FALSE, 0);
    GtkWidget *mic_actions = gtk_hbox_new(FALSE, 8);
    app->apply_source = action_button("Aplicar microfone", G_CALLBACK(apply_input), app);
    gtk_box_pack_start(GTK_BOX(mic_actions), app->apply_source, FALSE, FALSE, 0);
    gtk_box_pack_start(GTK_BOX(mic), mic_actions, FALSE, FALSE, 0);
    GtkWidget *gain_row = gtk_hbox_new(FALSE, 8);
    gtk_box_pack_start(GTK_BOX(gain_row), label("Ganho", FALSE), FALSE, FALSE, 0);
    app->source_scale = scale();
    gtk_box_pack_start(GTK_BOX(gain_row), app->source_scale, TRUE, TRUE, 0);
    app->apply_gain = action_button("Aplicar", G_CALLBACK(apply_gain), app);
    gtk_box_pack_start(GTK_BOX(gain_row), app->apply_gain, FALSE, FALSE, 0);
    gtk_box_pack_start(GTK_BOX(mic), gain_row, FALSE, FALSE, 0);
    gtk_box_pack_start(GTK_BOX(columns), panel(mic, "#fafafa"), TRUE, TRUE, 0);

    GtkWidget *output = gtk_vbox_new(FALSE, 9);
    gtk_box_pack_start(GTK_BOX(output), heading("Saída e volume", 11), FALSE, FALSE, 0);
    app->sink_combo = device_combo();
    gtk_box_pack_start(GTK_BOX(output), app->sink_combo, FALSE, FALSE, 0);
    app->apply_sink = action_button("Aplicar saída padrão", G_CALLBACK(apply_output), app);
    gtk_box_pack_start(GTK_BOX(output), app->apply_sink, FALSE, FALSE, 0);
    gtk_box_pack_start(GTK_BOX(output), label("Volume (%)", FALSE), FALSE, FALSE, 0);
    app->sink_scale = scale();
    gtk_box_pack_start(GTK_BOX(output), app->sink_scale, FALSE, FALSE, 0);
    app->mute = gtk_check_button_new_with_label("Silenciar");
    gtk_box_pack_start(GTK_BOX(output), app->mute, FALSE, FALSE, 0);
    app->apply_levels = action_button("Aplicar volume / silenciamento", G_CALLBACK(apply_levels), app);
    gtk_box_pack_start(GTK_BOX(output), app->apply_levels, FALSE, FALSE, 0);
    gtk_box_pack_start(GTK_BOX(columns), panel(output, "#fafafa"), TRUE, TRUE, 0);
    gtk_box_pack_start(GTK_BOX(page), columns, FALSE, FALSE, 0);

    GtkWidget *connection = gtk_hbox_new(FALSE, 18);
    GtkWidget *connection_text = gtk_vbox_new(FALSE, 9);
    gtk_box_pack_start(GTK_BOX(connection_text), heading("Conexão com o tablet · scrcpy", 11), FALSE, FALSE, 0);
    app->scrcpy_state = label("Estado ainda não consultado.", TRUE);
    gtk_box_pack_start(GTK_BOX(connection_text), app->scrcpy_state, FALSE, FALSE, 0);
    gtk_box_pack_start(GTK_BOX(connection), connection_text, TRUE, TRUE, 0);
    GtkWidget *connection_actions = gtk_vbox_new(FALSE, 9);
    GtkWidget *connection_buttons = gtk_hbox_new(FALSE, 8);
    app->connect = action_button("Conectar", G_CALLBACK(connect_clicked), app);
    app->disconnect = action_button("Desconectar", G_CALLBACK(disconnect_clicked), app);
    app->reconnect = action_button("Reconectar", G_CALLBACK(reconnect_clicked), app);
    gtk_box_pack_start(GTK_BOX(connection_buttons), app->connect, TRUE, TRUE, 0);
    gtk_box_pack_start(GTK_BOX(connection_buttons), app->disconnect, TRUE, TRUE, 0);
    gtk_box_pack_start(GTK_BOX(connection_buttons), app->reconnect, TRUE, TRUE, 0);
    app->serial = gtk_entry_new();
    app->scrcpy_path = gtk_entry_new();
    gtk_widget_set_tooltip_text(app->serial, "Serial ADB já autorizado ou IP:porta de conexão. A central não faz pareamento.");
    gtk_widget_set_tooltip_text(app->scrcpy_path, "Caminho opcional do scrcpy externo. Vazio: procurar no PATH.");
    GtkWidget *configuration = gtk_expander_new("Dispositivo ADB e scrcpy externo");
    GtkWidget *configuration_box = gtk_vbox_new(FALSE, 6);
    gtk_box_pack_start(GTK_BOX(configuration_box), label("Serial ADB autorizado", FALSE), FALSE, FALSE, 0);
    gtk_box_pack_start(GTK_BOX(configuration_box), app->serial, FALSE, FALSE, 0);
    gtk_box_pack_start(GTK_BOX(configuration_box), label("scrcpy externo (opcional: caminho)", FALSE), FALSE, FALSE, 0);
    gtk_box_pack_start(GTK_BOX(configuration_box), app->scrcpy_path, FALSE, FALSE, 0);
    gtk_container_add(GTK_CONTAINER(configuration), configuration_box);
    gtk_box_pack_start(GTK_BOX(connection_text), configuration, FALSE, FALSE, 0);
    gtk_box_pack_start(GTK_BOX(connection_actions), connection_buttons, FALSE, FALSE, 0);
    GtkWidget *dependencies = action_button("Verificar dependências", G_CALLBACK(dependencies_clicked), app);
    gtk_box_pack_start(GTK_BOX(connection_actions), dependencies, FALSE, FALSE, 0);
    gtk_box_pack_start(GTK_BOX(connection), connection_actions, FALSE, FALSE, 0);
    gtk_box_pack_start(GTK_BOX(page), panel(connection, "#fafafa"), FALSE, FALSE, 0);

    GtkWidget *restore_row = gtk_hbox_new(FALSE, 18);
    GtkWidget *restore_text = gtk_vbox_new(FALSE, 6);
    gtk_box_pack_start(GTK_BOX(restore_text), heading("Restaurar perfil de áudio", 11), FALSE, FALSE, 0);
    gtk_box_pack_start(GTK_BOX(restore_text), label("Recuperar entrada, saída, volumes e silenciamento guardados.", TRUE), FALSE, FALSE, 0);
    gtk_box_pack_start(GTK_BOX(restore_row), restore_text, TRUE, TRUE, 0);
    app->restore = action_button("Restaurar perfil", G_CALLBACK(restore_clicked), app);
    gtk_box_pack_start(GTK_BOX(restore_row), app->restore, FALSE, FALSE, 0);
    gtk_box_pack_start(GTK_BOX(page), panel(restore_row, "#fafafa"), FALSE, FALSE, 0);

    GtkWidget *bluetooth = gtk_hbox_new(FALSE, 18);
    GtkWidget *bluetooth_text = gtk_vbox_new(FALSE, 6);
    gtk_box_pack_start(GTK_BOX(bluetooth_text), heading("Bluetooth · transferência de arquivos", 11), FALSE, FALSE, 0);
    app->obex_state = label("Recurso separado do áudio. Compatibilidade e transferência ainda precisam de validação.", TRUE);
    gtk_box_pack_start(GTK_BOX(bluetooth_text), app->obex_state, FALSE, FALSE, 0);
    gtk_box_pack_start(GTK_BOX(bluetooth), bluetooth_text, TRUE, TRUE, 0);
    GtkWidget *obex_button = action_button("Verificar OBEX", G_CALLBACK(obex_clicked), app);
    gtk_box_pack_start(GTK_BOX(bluetooth), obex_button, FALSE, FALSE, 0);
    gtk_box_pack_start(GTK_BOX(page), panel(bluetooth, "#e9ebed"), FALSE, FALSE, 0);
    gtk_box_pack_start(GTK_BOX(page), label(
        "Trocas de perfil são bloqueadas durante captura. Volumes só mudam ao clicar em Aplicar.", TRUE), FALSE, FALSE, 0);

    g_signal_connect(app->tablet_radio, "toggled", G_CALLBACK(input_changed), app);
    g_signal_connect(app->local_radio, "toggled", G_CALLBACK(input_changed), app);
    g_signal_connect(app->local_combo, "changed", G_CALLBACK(input_changed), app);
    g_signal_connect(app->sink_combo, "changed", G_CALLBACK(output_changed), app);
    gtk_widget_show_all(app->window);
    refresh(app);
    gtk_main();
    g_ptr_array_free(app->sources, TRUE);
    g_ptr_array_free(app->sinks, TRUE);
    g_free(app->tablet);
    g_free(app->helper);
    g_free(app);
    return 0;
}

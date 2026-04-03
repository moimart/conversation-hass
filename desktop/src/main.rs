use gtk4::prelude::*;
use gtk4::{
    gdk, glib, Application, ApplicationWindow, Box as GtkBox, CssProvider, Entry, Label,
    Orientation,
};
use gtk4_layer_shell::{Edge, KeyboardMode, Layer, LayerShell};
use serde::Deserialize;
use std::cell::Cell;
use std::path::PathBuf;
use std::rc::Rc;

const APP_ID: &str = "com.hal.command";
const DEFAULT_CSS: &str = include_str!("../config/style.css");

#[derive(Deserialize)]
struct Config {
    server: ServerConfig,
    ui: Option<UiConfig>,
}

#[derive(Deserialize)]
struct ServerConfig {
    url: String,
}

#[derive(Deserialize, Default)]
struct UiConfig {
    width: Option<i32>,
    top_margin: Option<i32>,
    dismiss_delay: Option<u64>,
    placeholder: Option<String>,
}

fn config_dir() -> PathBuf {
    dirs::config_dir()
        .unwrap_or_else(|| PathBuf::from("~/.config"))
        .join("hal-command")
}

fn load_config() -> Config {
    let path = config_dir().join("config.toml");
    if let Ok(contents) = std::fs::read_to_string(&path) {
        if let Ok(cfg) = toml::from_str(&contents) {
            return cfg;
        }
        eprintln!("Warning: Failed to parse {}, using defaults", path.display());
    }

    // Fall back to env var or default
    let url = std::env::var("HAL_SERVER_URL")
        .unwrap_or_else(|_| "http://localhost:8765".to_string());
    Config {
        server: ServerConfig { url },
        ui: Some(UiConfig::default()),
    }
}

fn load_css() -> String {
    let path = config_dir().join("style.css");
    std::fs::read_to_string(&path).unwrap_or_else(|_| DEFAULT_CSS.to_string())
}

fn send_command(url: &str, text: &str) -> bool {
    let rt = tokio::runtime::Runtime::new().unwrap();
    rt.block_on(async {
        reqwest::Client::new()
            .post(format!("{url}/api/command"))
            .json(&serde_json::json!({"text": text}))
            .send()
            .await
            .map(|r| r.status().is_success())
            .unwrap_or(false)
    })
}

fn main() {
    let config = load_config();
    let app = Application::builder().application_id(APP_ID).build();

    let url = config.server.url.clone();
    let ui = config.ui.unwrap_or_default();
    let width = ui.width.unwrap_or(500);
    let top_margin = ui.top_margin.unwrap_or(300);
    let dismiss_delay = ui.dismiss_delay.unwrap_or(400);
    let placeholder = ui.placeholder.unwrap_or_else(|| "Command HAL...".to_string());

    app.connect_activate(move |app| {
        build_ui(app, &url, width, top_margin, dismiss_delay, &placeholder)
    });
    app.run();
}

fn build_ui(
    app: &Application,
    server_url: &str,
    width: i32,
    top_margin: i32,
    dismiss_delay: u64,
    placeholder: &str,
) {
    let window = ApplicationWindow::builder()
        .application(app)
        .default_width(width)
        .default_height(60)
        .decorated(false)
        .resizable(false)
        .build();

    // Layer shell: overlay centered on screen
    window.init_layer_shell();
    window.set_layer(Layer::Overlay);
    window.set_keyboard_mode(KeyboardMode::Exclusive);
    window.set_anchor(Edge::Top, false);
    window.set_anchor(Edge::Bottom, false);
    window.set_anchor(Edge::Left, false);
    window.set_anchor(Edge::Right, false);
    window.set_margin(Edge::Top, top_margin);

    // Load CSS
    let css = CssProvider::new();
    css.load_from_data(&load_css());
    gtk4::style_context_add_provider_for_display(
        &gdk::Display::default().unwrap(),
        &css,
        gtk4::STYLE_PROVIDER_PRIORITY_APPLICATION,
    );

    // Layout
    let container = GtkBox::new(Orientation::Horizontal, 0);
    container.add_css_class("hal-container");

    let icon = Label::new(Some("\u{25CF}")); // ●
    icon.add_css_class("hal-icon");
    container.append(&icon);

    let entry = Entry::new();
    entry.set_hexpand(true);
    entry.set_placeholder_text(Some(placeholder));
    entry.add_css_class("hal-entry");
    container.append(&entry);

    let status = Label::new(None);
    status.set_visible(false);
    container.append(&status);

    window.set_child(Some(&container));

    // ESC to close
    let key_ctrl = gtk4::EventControllerKey::new();
    let win_ref = window.clone();
    let entry_esc = entry.clone();
    key_ctrl.connect_key_pressed(move |_, key, _, _| {
        if key == gdk::Key::Escape {
            entry_esc.set_focusable(false);
            win_ref.close();
            glib::Propagation::Stop
        } else {
            glib::Propagation::Proceed
        }
    });
    window.add_controller(key_ctrl);

    // Enter to send
    let url = Rc::new(server_url.to_string());
    let busy = Rc::new(Cell::new(false));

    let win = window.clone();
    let st = status.clone();
    let en = entry.clone();
    let u = url.clone();
    let b = busy.clone();

    entry.connect_activate(move |_| {
        let text = en.text().trim().to_string();
        if text.is_empty() || b.get() {
            return;
        }

        b.set(true);
        en.set_sensitive(false);

        let url_c = u.to_string();
        let st_c = st.clone();
        let win_c = win.clone();
        let en_c = en.clone();
        let b_c = b.clone();

        let (tx, rx) = async_channel::bounded::<bool>(1);
        let text_c = text.clone();

        std::thread::spawn(move || {
            let ok = send_command(&url_c, &text_c);
            let _ = tx.send_blocking(ok);
        });

        glib::spawn_future_local(async move {
            let success = rx.recv().await.unwrap_or(false);
            b_c.set(false);

            if success {
                st_c.set_label("\u{2713}"); // ✓
                st_c.remove_css_class("hal-error");
                st_c.add_css_class("hal-success");
                st_c.set_visible(true);
                en_c.set_focusable(false);
                let w = win_c.clone();
                glib::timeout_add_local_once(
                    std::time::Duration::from_millis(dismiss_delay),
                    move || w.close(),
                );
            } else {
                st_c.set_label("\u{2717}"); // ✗
                st_c.remove_css_class("hal-success");
                st_c.add_css_class("hal-error");
                st_c.set_visible(true);
                en_c.set_sensitive(true);
            }
        });
    });

    window.present();
    entry.grab_focus();
}

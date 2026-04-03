use gtk4::prelude::*;
use gtk4::{
    gdk, glib, Application, ApplicationWindow, Box as GtkBox, CssProvider, Entry, Label,
    Orientation,
};
use gtk4_layer_shell::{Edge, KeyboardMode, Layer, LayerShell};
use std::cell::Cell;
use std::rc::Rc;

const APP_ID: &str = "com.hal.command";

fn main() {
    let server_url = std::env::var("HAL_SERVER_URL")
        .unwrap_or_else(|_| "http://localhost:8765".to_string());

    let app = Application::builder().application_id(APP_ID).build();

    let url = server_url.clone();
    app.connect_activate(move |app| build_ui(app, &url));
    app.run();
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

fn build_ui(app: &Application, server_url: &str) {
    let window = ApplicationWindow::builder()
        .application(app)
        .default_width(500)
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
    window.set_margin(Edge::Top, 300);

    // CSS
    let css = CssProvider::new();
    css.load_from_data(
        r#"
        window {
            background-color: rgba(10, 10, 12, 0.92);
            border-radius: 16px;
            border: 1px solid rgba(255, 45, 45, 0.3);
        }
        .hal-container {
            padding: 12px 16px;
        }
        .hal-entry {
            background-color: rgba(20, 20, 24, 0.9);
            color: #e8e8ec;
            border: 1px solid rgba(255, 45, 45, 0.2);
            border-radius: 8px;
            padding: 8px 12px;
            font-family: "JetBrains Mono", "Fira Code", monospace;
            font-size: 15px;
            caret-color: #ff4444;
        }
        .hal-entry:focus {
            border-color: rgba(255, 45, 45, 0.5);
        }
        .hal-icon {
            color: #ff2d2d;
            font-size: 18px;
            margin-right: 10px;
        }
        .hal-success {
            color: #22c55e;
            font-size: 18px;
            margin-left: 10px;
        }
        .hal-error {
            color: #ff2d2d;
            font-size: 18px;
            margin-left: 10px;
        }
        "#,
    );
    gtk4::style_context_add_provider_for_display(
        &gdk::Display::default().unwrap(),
        &css,
        gtk4::STYLE_PROVIDER_PRIORITY_APPLICATION,
    );

    // Layout
    let container = GtkBox::new(Orientation::Horizontal, 0);
    container.add_css_class("hal-container");

    let icon = Label::new(Some("●"));
    icon.add_css_class("hal-icon");
    container.append(&icon);

    let entry = Entry::new();
    entry.set_hexpand(true);
    entry.set_placeholder_text(Some("Command HAL..."));
    entry.add_css_class("hal-entry");
    container.append(&entry);

    let status = Label::new(None);
    status.set_visible(false);
    container.append(&status);

    window.set_child(Some(&container));

    // ESC to close
    let key_ctrl = gtk4::EventControllerKey::new();
    let win_ref = window.clone();
    key_ctrl.connect_key_pressed(move |_, key, _, _| {
        if key == gdk::Key::Escape {
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

        // Run HTTP request in a thread, signal back via channel
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
                st_c.set_label("✓");
                st_c.remove_css_class("hal-error");
                st_c.add_css_class("hal-success");
                st_c.set_visible(true);
                let w = win_c.clone();
                glib::timeout_add_local_once(std::time::Duration::from_millis(400), move || {
                    w.close();
                });
            } else {
                st_c.set_label("✗");
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

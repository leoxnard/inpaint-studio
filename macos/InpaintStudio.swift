// Inpaint Studio launcher: a native window (WKWebView) around the local server.
// The app code is bundled in Contents/Resources/app. On start it installs uv + the app's Python deps
// if needed, starts the server (which starts ComfyUI headless when needed) and shows the UI in its own
// window instead of a browser tab. Closing the window keeps the server running (Dock click reopens it);
// quitting stops the server, which also stops that ComfyUI.

import AppKit
import WebKit

let port = 7380
let base = URL(string: "http://127.0.0.1:\(port)/")!
let home = NSHomeDirectory()
let supportDir = home + "/Library/Application Support/Inpaint Studio"
let logFile = home + "/Library/Logs/InpaintStudio.log"
let pathPrefix = "export PATH=\"$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH\""

@discardableResult
func shell(_ cmd: String) -> Int32 {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/bin/bash")
    p.arguments = ["-c", cmd]
    p.standardInput = FileHandle.nullDevice
    p.standardOutput = FileHandle.nullDevice
    p.standardError = FileHandle.nullDevice
    do { try p.run() } catch { return -1 }
    p.waitUntilExit()
    return p.terminationStatus
}

/// Blocking GET against the local server (call off the main thread, or with a short timeout).
func get(_ path: String, timeout: TimeInterval = 1) -> (status: Int, data: Data?) {
    var req = URLRequest(url: URL(string: path, relativeTo: base)!)
    req.timeoutInterval = timeout
    var result: (Int, Data?) = (0, nil)
    let done = DispatchSemaphore(value: 0)
    URLSession.shared.dataTask(with: req) { data, resp, _ in
        result = ((resp as? HTTPURLResponse)?.statusCode ?? 0, data)
        done.signal()
    }.resume()
    done.wait()
    return result
}

func serverUp() -> Bool { get("healthz").status == 200 }

func statusPage(_ text: String) -> String {
    """
    <html><body style="margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
    font:15px -apple-system,sans-serif;color:#555;background:#fff">\(text)</body></html>
    """
}

class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, WKDownloadDelegate {
    var window: NSWindow!
    var web: WKWebView!
    var titleObservation: NSKeyValueObservation?
    var ready = false

    func applicationDidFinishLaunching(_ note: Notification) {
        buildMenu()
        let config = WKWebViewConfiguration()
        config.preferences.setValue(true, forKey: "developerExtrasEnabled")
        web = WKWebView(frame: .zero, configuration: config)
        web.navigationDelegate = self
        web.uiDelegate = self
        if #available(macOS 13.3, *) { web.isInspectable = true }

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1440, height: 900),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "Inpaint Studio"
        window.contentView = web
        window.isReleasedWhenClosed = false
        window.minSize = NSSize(width: 480, height: 400)
        window.center()
        window.setFrameAutosaveName("main")
        window.makeKeyAndOrderFront(nil)
        titleObservation = web.observe(\.title) { [weak self] web, _ in
            if let t = web.title, !t.isEmpty { self?.window.title = t }
        }
        NSApp.activate(ignoringOtherApps: true)

        web.loadHTMLString(statusPage("Starting Inpaint Studio…"), baseURL: nil)
        DispatchQueue.global().async { self.start() }
    }

    func status(_ text: String) {
        DispatchQueue.main.async { self.web.loadHTMLString(statusPage(text), baseURL: nil) }
    }

    func fail(_ msg: String) {
        DispatchQueue.main.async {
            let a = NSAlert()
            a.alertStyle = .critical
            a.messageText = "Inpaint Studio could not start"
            a.informativeText = msg + "\n\nLog: ~/Library/Logs/InpaintStudio.log"
            a.runModal()
            NSApp.terminate(nil)
        }
    }

    func start() {
        if !serverUp() {
            let appDir = Bundle.main.resourcePath! + "/app"
            let env = pathPrefix + " UV_PROJECT_ENVIRONMENT=\"\(supportDir)/venv\""
                + " && mkdir -p \"\(supportDir)\" \"$HOME/Library/Logs\" && cd '\(appDir)'"
            if shell(pathPrefix + " && command -v uv") != 0 {
                status("Installing uv (Python tool manager)…")
                if shell("curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh") != 0 {
                    return fail("Installing uv failed.")
                }
            }
            status("Installing app dependencies (first start can take a minute)…")
            if shell(env + " && uv sync --frozen --no-dev -q >> \"\(logFile)\" 2>&1") != 0 {
                return fail("Installing the app dependencies failed.")
            }
            status("Starting server…")
            // fully detached, so the server is not tied to this process's lifetime or pipes
            shell(env + " && (nohup \"\(supportDir)/venv/bin/python\" -m uvicorn server:app --host 127.0.0.1"
                  + " --port \(port) >> \"\(logFile)\" 2>&1 < /dev/null &)")
            for _ in 0..<60 where !serverUp() { Thread.sleep(forTimeInterval: 0.5) }
            if !serverUp() { return fail("The server did not come up.") }
        }
        DispatchQueue.main.async {
            self.ready = true
            self.web.load(URLRequest(url: base))
        }
    }

    // Dock click with the window closed: show it again
    func applicationShouldHandleReopen(_ app: NSApplication, hasVisibleWindows: Bool) -> Bool {
        window.makeKeyAndOrderFront(nil)
        return true
    }

    func applicationShouldTerminate(_ app: NSApplication) -> NSApplication.TerminateReply {
        if ready, let data = get("api/jobs", timeout: 2).data,
           let jobs = try? JSONSerialization.jsonObject(with: data) as? [Any], !jobs.isEmpty {
            let a = NSAlert()
            a.alertStyle = .warning
            a.messageText = "\(jobs.count) job(s) are still queued or running"
            a.informativeText = "Quitting stops the server (and the ComfyUI it started), so they will be lost."
            a.addButton(withTitle: "Keep running")
            a.addButton(withTitle: "Quit anyway")
            if a.runModal() == .alertFirstButtonReturn { return .terminateCancel }
        }
        // SIGTERM lets the server shut down cleanly; its shutdown hook stops the ComfyUI it started
        shell("pkill -f 'uvicorn server:app.*--port \(port)' || true; for i in $(seq 1 30); do "
              + "curl -s -o /dev/null --max-time 1 http://127.0.0.1:\(port)/healthz || break; sleep 0.5; done")
        return .terminateNow
    }

    // MARK: web view

    // Links to other sites (model pages, docs) open in the default browser
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if action.shouldPerformDownload { return decisionHandler(.download) }
        if let url = action.request.url, ["http", "https"].contains(url.scheme ?? ""),
           url.host != base.host, action.navigationType == .linkActivated {
            NSWorkspace.shared.open(url)
            return decisionHandler(.cancel)
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, decidePolicyFor response: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        decisionHandler(response.canShowMIMEType ? .allow : .download)
    }

    // target=_blank / window.open: local pages in this window, others in the browser
    func webView(_ webView: WKWebView, createWebViewWith config: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url {
            if url.host == base.host { webView.load(action.request) } else { NSWorkspace.shared.open(url) }
        }
        return nil
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
        download.delegate = self
    }

    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
        download.delegate = self
    }

    // Downloads go straight to ~/Downloads (like a browser), with " 2", " 3" … on name clashes
    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse,
                  suggestedFilename: String, completionHandler: @escaping (URL?) -> Void) {
        let dir = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask)[0]
        let name = suggestedFilename as NSString
        var url = dir.appendingPathComponent(suggestedFilename)
        var n = 2
        while FileManager.default.fileExists(atPath: url.path) {
            let ext = name.pathExtension.isEmpty ? "" : "." + name.pathExtension
            url = dir.appendingPathComponent("\(name.deletingPathExtension) \(n)\(ext)")
            n += 1
        }
        completionHandler(url)
    }

    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.canChooseDirectories = parameters.allowsDirectories
        panel.beginSheetModal(for: window) { completionHandler($0 == .OK ? panel.urls : nil) }
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let a = NSAlert()
        a.messageText = message
        a.runModal()
        completionHandler()
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let a = NSAlert()
        a.messageText = message
        a.addButton(withTitle: "OK")
        a.addButton(withTitle: "Cancel")
        completionHandler(a.runModal() == .alertFirstButtonReturn)
    }

    // MARK: menu (without an Edit menu, ⌘C / ⌘V do not work in the web view)

    @objc func reload() { if ready { web.reload() } }
    @objc func zoomIn() { web.pageZoom = min(web.pageZoom + 0.1, 3) }
    @objc func zoomOut() { web.pageZoom = max(web.pageZoom - 0.1, 0.5) }
    @objc func zoomReset() { web.pageZoom = 1 }
    @objc func openInBrowser() { NSWorkspace.shared.open(web.url?.scheme == "http" ? web.url! : base) }

    func buildMenu() {
        func item(_ title: String, _ action: Selector?, _ key: String = "",
                  _ mods: NSEvent.ModifierFlags = .command, target: AnyObject? = nil) -> NSMenuItem {
            let i = NSMenuItem(title: title, action: action, keyEquivalent: key)
            i.keyEquivalentModifierMask = mods
            i.target = target
            return i
        }
        func menu(_ title: String, _ items: [NSMenuItem]) -> NSMenuItem {
            let m = NSMenu(title: title)
            items.forEach(m.addItem)
            let top = NSMenuItem()
            top.submenu = m
            return top
        }
        let main = NSMenu()
        main.addItem(menu("Inpaint Studio", [
            item("Hide Inpaint Studio", #selector(NSApplication.hide(_:)), "h"),
            item("Hide Others", #selector(NSApplication.hideOtherApplications(_:)), "h", [.command, .option]),
            .separator(),
            item("Quit Inpaint Studio", #selector(NSApplication.terminate(_:)), "q"),
        ]))
        main.addItem(menu("Edit", [
            item("Undo", Selector(("undo:")), "z"),
            item("Redo", Selector(("redo:")), "z", [.command, .shift]),
            .separator(),
            item("Cut", #selector(NSText.cut(_:)), "x"),
            item("Copy", #selector(NSText.copy(_:)), "c"),
            item("Paste", #selector(NSText.paste(_:)), "v"),
            item("Select All", #selector(NSText.selectAll(_:)), "a"),
        ]))
        main.addItem(menu("View", [
            item("Reload", #selector(reload), "r", target: self),
            item("Open in Browser", #selector(openInBrowser), "o", [.command, .shift], target: self),
            .separator(),
            item("Actual Size", #selector(zoomReset), "0", target: self),
            item("Zoom In", #selector(zoomIn), "+", target: self),
            item("Zoom Out", #selector(zoomOut), "-", target: self),
            .separator(),
            item("Enter Full Screen", #selector(NSWindow.toggleFullScreen(_:)), "f", [.command, .control]),
        ]))
        main.addItem(menu("Window", [
            item("Close", #selector(NSWindow.performClose(_:)), "w"),
            item("Minimize", #selector(NSWindow.performMiniaturize(_:)), "m"),
        ]))
        NSApp.mainMenu = main
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()

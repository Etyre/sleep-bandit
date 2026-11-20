// Sleep Bandit for iPhone: a native shell around the web app.
//
// The app is one full-screen web view on the Sleep Bandit server (SBServerURL in
// Info.plist, set in project.yml: the tailnet-only HTTPS address). All of the
// interface is the server's own page, so a change deployed to the server shows up
// here on the next load with no rebuild; this shell only needs rebuilding when
// this file or project.yml changes (ios/build-ios.sh).
//
// What the shell adds over a Safari tab:
//   - its own icon and a full-screen window, opening on the Tonight view
//   - a plain "can't reach it" screen (Tailscale off, server down) with a retry,
//     and an automatic retry when the app comes back to the foreground
//   - pull down to reload
//   - the page's alert / confirm / prompt dialogs (a bare web view drops them)
//   - links that leave the server (Export, Oura) open in Safari
// The server's host is an app-bound domain (WKAppBoundDomains), which is what lets
// the page's service worker run here, so the last plan stays readable offline.

import SwiftUI
import WebKit

@main
struct SleepBanditApp: App {
    var body: some Scene {
        WindowGroup {
            RootView().preferredColorScheme(.dark)
        }
    }
}

enum Config {
    /// Where the app lives; the fragment opens the Tonight view.
    static let home: URL = {
        let s = Bundle.main.object(forInfoDictionaryKey: "SBServerURL") as? String ?? ""
        guard let u = URL(string: s), u.host != nil else { fatalError("SBServerURL missing from Info.plist") }
        return u
    }()
    static let background = UIColor(red: 0x0F / 255, green: 0x12 / 255, blue: 0x20 / 255, alpha: 1)
    static let loadTimeout: TimeInterval = 15   // a tailnet address just hangs when Tailscale is off
}

final class WebModel: NSObject, ObservableObject, WKNavigationDelegate, WKUIDelegate {
    @Published var failure: String?      // non-nil: the page could not be loaded
    @Published var firstLoad = true      // nothing on screen yet
    let webView: WKWebView
    private let refresh = UIRefreshControl()

    override init() {
        let cfg = WKWebViewConfiguration()
        cfg.limitsNavigationsToAppBoundDomains = true   // required for service workers in a WKWebView
        webView = WKWebView(frame: .zero, configuration: cfg)
        super.init()
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.isOpaque = false
        webView.backgroundColor = Config.background
        webView.underPageBackgroundColor = Config.background
        webView.scrollView.backgroundColor = Config.background
        // the page lays itself out under the notch and home bar (viewport-fit=cover + env(safe-area-inset-*))
        webView.scrollView.contentInsetAdjustmentBehavior = .never
        refresh.tintColor = .lightGray
        refresh.addTarget(self, action: #selector(pulled), for: .valueChanged)
        webView.scrollView.refreshControl = refresh
        load()
    }

    func load() {
        failure = nil
        if webView.url == nil {
            webView.load(URLRequest(url: Config.home, timeoutInterval: Config.loadTimeout))
        } else {
            webView.reload()
        }
    }

    @objc private func pulled() { load() }

    /// Back in the foreground: a failed load is retried without a tap.
    func becameActive() {
        if failure != nil { load() }
    }

    // MARK: navigation

    private func isOurs(_ url: URL) -> Bool {
        url.host == Config.home.host && url.port == Config.home.port
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else { return decisionHandler(.cancel) }
        let mainFrame = action.targetFrame?.isMainFrame ?? false
        // a new-window link (Export) or anything leaving the server (Oura's sign-in) goes to Safari
        if action.targetFrame == nil || (mainFrame && !isOurs(url) && ["http", "https"].contains(url.scheme ?? "")) {
            UIApplication.shared.open(url)
            return decisionHandler(.cancel)
        }
        // mailto:, tel: and the like: the web view cannot load them and would show the "can't reach" screen
        if mainFrame && !["http", "https", "about", "blob", "data"].contains((url.scheme ?? "").lowercased()) {
            UIApplication.shared.open(url)
            return decisionHandler(.cancel)
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        failure = nil
        firstLoad = false
        refresh.endRefreshing()
    }

    private func failed(_ error: Error) {
        refresh.endRefreshing()
        let e = error as NSError
        if e.domain == NSURLErrorDomain && e.code == NSURLErrorCancelled { return }   // superseded by another load
        if e.domain == "WebKitErrorDomain" && e.code == 102 { return }               // we handed the link to Safari
        failure = e.localizedDescription
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        failed(error)
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        failed(error)
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        load()   // iOS reclaimed the page's memory while the app was in the background
    }

    // MARK: the page's own dialogs

    private var presenter: UIViewController? {
        let scene = UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first
        var vc = scene?.keyWindow?.rootViewController
        while let next = vc?.presentedViewController { vc = next }
        return vc
    }

    private func present(_ alert: UIAlertController, otherwise: () -> Void) {
        if let p = presenter { p.present(alert, animated: true) } else { otherwise() }
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let a = UIAlertController(title: nil, message: message, preferredStyle: .alert)
        a.addAction(UIAlertAction(title: "OK", style: .default) { _ in completionHandler() })
        present(a, otherwise: completionHandler)
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let a = UIAlertController(title: nil, message: message, preferredStyle: .alert)
        a.addAction(UIAlertAction(title: "Cancel", style: .cancel) { _ in completionHandler(false) })
        a.addAction(UIAlertAction(title: "OK", style: .default) { _ in completionHandler(true) })
        present(a) { completionHandler(false) }
    }

    func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String, defaultText: String?,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (String?) -> Void) {
        let a = UIAlertController(title: nil, message: prompt, preferredStyle: .alert)
        a.addTextField { $0.text = defaultText }
        a.addAction(UIAlertAction(title: "Cancel", style: .cancel) { _ in completionHandler(nil) })
        a.addAction(UIAlertAction(title: "OK", style: .default) { _ in completionHandler(a.textFields?.first?.text) })
        present(a) { completionHandler(nil) }
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url { UIApplication.shared.open(url) }   // window.open -> Safari
        return nil
    }
}

struct WebView: UIViewRepresentable {
    let model: WebModel
    func makeUIView(context: Context) -> WKWebView { model.webView }
    func updateUIView(_ view: WKWebView, context: Context) {}
}

struct RootView: View {
    @StateObject private var model = WebModel()
    @Environment(\.scenePhase) private var phase

    var body: some View {
        ZStack {
            Color(Config.background).ignoresSafeArea()
            WebView(model: model).ignoresSafeArea()
            if let why = model.failure {
                Unreachable(why: why) { model.load() }
            } else if model.firstLoad {
                ProgressView().tint(.gray)
            }
        }
        .onChange(of: phase) { _, now in
            if now == .active { model.becameActive() }
        }
    }
}

/// Shown instead of a blank page when the server did not answer.
struct Unreachable: View {
    let why: String
    let retry: () -> Void

    var body: some View {
        VStack(spacing: 14) {
            Image(systemName: "moon.zzz.fill")
                .font(.system(size: 44))
                .foregroundStyle(Color(red: 0x5E / 255, green: 0xE0 / 255, blue: 0xC0 / 255))
            Text("Can't reach Sleep Bandit")
                .font(.title3.weight(.semibold))
            Text("It lives on your private Tailscale network. Check that Tailscale is connected on this phone, then try again.")
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
            Button("Try again", action: retry)
                .buttonStyle(.borderedProminent)
                .padding(.top, 4)
            Text(why)
                .font(.caption)
                .foregroundStyle(.tertiary)
                .multilineTextAlignment(.center)
        }
        .padding(32)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(Config.background).ignoresSafeArea())
    }
}

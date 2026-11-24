import AppKit
import WebKit
import Security
import UniformTypeIdentifiers

final class Client: NSObject, NSApplicationDelegate, WKScriptMessageHandler, WKNavigationDelegate {
    var window: NSWindow!
    var web: WKWebView!
    var child: Process?
    var input: FileHandle?
    var outputBuffer = Data()
    let ioQueue = DispatchQueue(label: "local.tgcopytrader.io")
    var queuedScripts: [String] = []
    var pageReady = false
    var stopping = false
    var startupError: String?
    var secretWrites: [Int:String] = [:]
    var accountWrites: [Int:String] = [:]
    var accountChanges = Set<Int>()
    var preparedSummaries: [String:String] = [:]
    var confirmationOpen = false
    let bundleID = "local.tgcopytrader.client"
    var keychainService: String { bundleID + (ProcessInfo.processInfo.environment["TG_COPYTRADER_DATA_DIR"] == nil ? "" : ".test") }
    var root: URL {
        if let custom = ProcessInfo.processInfo.environment["TG_COPYTRADER_DATA_DIR"] { return URL(fileURLWithPath: custom) }
        return FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("TG信号跟单", isDirectory: true)
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        if ProcessInfo.processInfo.environment["TG_COPYTRADER_DATA_DIR"] == nil,
           let existing = NSRunningApplication.runningApplications(withBundleIdentifier: bundleID).first(where: { $0.processIdentifier != ProcessInfo.processInfo.processIdentifier }) {
            existing.activate(options: [.activateAllWindows]); NSApp.terminate(nil); return
        }
        let menu = NSMenu()
        let appItem = NSMenuItem(); menu.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "关于信号跟单", action: #selector(about), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "退出信号跟单", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        let editItem = NSMenuItem(); editItem.title = "编辑"; menu.addItem(editItem)
        let edit = NSMenu(title: "编辑")
        for (name, action, key) in [("撤销", "undo:", "z"),("重做", "redo:", "Z"),("剪切", "cut:", "x"),("复制", "copy:", "c"),("粘贴", "paste:", "v"),("全选", "selectAll:", "a")] {
            edit.addItem(withTitle: name, action: Selector(action), keyEquivalent: key)
        }
        editItem.submenu = edit; NSApp.mainMenu = menu
        let content = WKUserContentController(); content.add(self, name: "native")
        let config = WKWebViewConfiguration(); config.userContentController = content
        config.websiteDataStore = .nonPersistent()
        web = WKWebView(frame: .zero, configuration: config)
        web.navigationDelegate = self
        web.setValue(false, forKey: "drawsBackground")
        window = NSWindow(contentRect: NSRect(x: 0,y: 0,width: 1260,height: 840), styleMask: [.titled,.closable,.miniaturizable,.resizable], backing: .buffered, defer: false)
        window.title = "信号跟单 · ForeGate API 工作台"
        window.appearance = NSAppearance(named: .aqua)
        window.backgroundColor = NSColor(srgbRed: 248 / 255, green: 246 / 255, blue: 245 / 255, alpha: 1)
        window.minSize = NSSize(width: 1050,height: 720)
        window.contentView = web; window.center(); window.isReleasedWhenClosed = false
        startBackend()
        guard let resources = Bundle.main.resourceURL else { return }
        let ui = resources.appendingPathComponent("app/ui/api.html")
        web.loadFileURL(ui, allowingReadAccessTo: resources.appendingPathComponent("app/ui"))
        window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
    }

    @objc func about() {
        let a = NSAlert(); a.messageText = "信号跟单 " + (Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "")
        a.informativeText = "ForeGate API 账户管理、订单簿交易和 Telegram 自动跟单。"
        a.runModal()
    }

    func secretQuery(_ account: String) -> [String:Any] {
        return [kSecClass as String:kSecClassGenericPassword,kSecAttrService as String:keychainService,kSecAttrAccount as String:account]
    }

    func readSecret(_ account: String = "telegram-api-hash") -> String {
        var q = secretQuery(account)
        q[kSecReturnData as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        var result:CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary,&result) == errSecSuccess, let data = result as? Data else { return "" }
        return String(data: data, encoding: .utf8) ?? ""
    }

    func storeSecret(_ value: String, account: String = "telegram-api-hash") -> Bool {
        let q = secretQuery(account)
        let attrs:[String:Any] = [kSecValueData as String:Data(value.utf8)]
        let code = SecItemUpdate(q as CFDictionary, attrs as CFDictionary)
        if code == errSecSuccess { return true }
        guard code == errSecItemNotFound else { return false }
        var add=q; add[kSecValueData as String]=Data(value.utf8);add[kSecAttrAccessible as String]=kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        return SecItemAdd(add as CFDictionary,nil) == errSecSuccess
    }

    func startBackend() {
        do {
            try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true, attributes: [.posixPermissions:0o700])
            guard let resources=Bundle.main.resourceURL else { throw NSError(domain:"app",code:1) }
            let runtime=resources.appendingPathComponent("runtime")
            let p=Process();p.executableURL=runtime.appendingPathComponent("bin/python3.12")
            p.arguments=["-u","-m","copytrader.desktop_service","--data-dir",root.path,"--api-client"]
            p.currentDirectoryURL=resources.appendingPathComponent("app")
            var env=ProcessInfo.processInfo.environment
            env["PYTHONHOME"]=runtime.path;env["PYTHONPATH"]=resources.appendingPathComponent("vendor").path+":"+resources.appendingPathComponent("app").path
            env["PYTHONDONTWRITEBYTECODE"]="1";env["PYTHONNOUSERSITE"]="1";env["PYTHONUNBUFFERED"]="1"
            p.environment=env
            let stdinPipe=Pipe(), stdoutPipe=Pipe(), stderrPipe=Pipe()
            p.standardInput=stdinPipe;p.standardOutput=stdoutPipe;p.standardError=stderrPipe
            input=stdinPipe.fileHandleForWriting
            stdoutPipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
                let data=handle.availableData
                if data.isEmpty { return }
                self?.ioQueue.async {
                    guard let self=self else { return }
                    self.outputBuffer.append(data)
                    while let range=self.outputBuffer.firstRange(of: Data([10])) {
                        let line=self.outputBuffer.subdata(in:0..<range.lowerBound)
                        self.outputBuffer.removeSubrange(0..<range.upperBound)
                        DispatchQueue.main.async { self.receive(line) }
                    }
                }
            }
            // Drain diagnostics without ever surfacing raw service output or secrets.
            stderrPipe.fileHandleForReading.readabilityHandler = { handle in _ = handle.availableData }
            p.terminationHandler = { [weak self] _ in DispatchQueue.main.async {
                guard let self=self,!self.stopping else{return}
                self.startupError="本地服务已停止，请退出客户端后重新打开。"
                self.web.evaluateJavaScript("toast('本地服务已停止，请重新打开客户端。',true)")
            }}
            try p.run();child=p
            write(["id":-1,"action":"credentials","data":["api_hash":readSecret()]])
        } catch {
            startupError="客户端运行组件未能启动。请保留此窗口并联系项目维护。"
        }
    }

    func write(_ request:[String:Any]) {
        guard let data=try? JSONSerialization.data(withJSONObject:request), var inputData=Optional(data) else{return}
        inputData.append(10)
        do { try input?.write(contentsOf:inputData) } catch { startupError="无法连接本地服务。" }
    }

    func respond(_ object:[String:Any]) {
        guard let data=try? JSONSerialization.data(withJSONObject:object),let json=String(data:data,encoding:.utf8) else{return}
        let script="window.receive(\(json));"
        if pageReady { web.evaluateJavaScript(script) } else { queuedScripts.append(script) }
    }

    func receive(_ data:Data) {
        guard var object=(try? JSONSerialization.jsonObject(with:data)) as? [String:Any] else{return}
        let id=(object["id"] as? NSNumber)?.intValue ?? 0
        if id < 0 { return }
        if accountChanges.remove(id) != nil {
            let staged = accountWrites.removeValue(forKey: id)
            if object["ok"] as? Bool == true, var result = object["data"] as? [String:Any] {
                if let retired = result.removeValue(forKey: "retired_slot") as? String {
                    SecItemDelete(secretQuery("foregate-" + retired) as CFDictionary)
                }
                preparedSummaries.removeAll()
                object["data"] = result
            } else if let staged = staged {
                SecItemDelete(secretQuery("foregate-" + staged) as CFDictionary)
            }
        }
        if let secret=secretWrites.removeValue(forKey:id), object["ok"] as? Bool == true {
            if storeSecret(secret) {
                write(["id":-2,"action":"credentials","data":["api_hash":secret]])
                if var data=object["data"] as? [String:Any],var settings=data["settings"] as? [String:Any] {
                    settings["api_hash_set"]=true;data["settings"]=settings;object["data"]=data
                }
            } else {
                object=["id":id,"ok":false,"error":"连接参数已保存，但 API Hash 未能写入钥匙串。请检查系统钥匙串权限后重新保存。"]
            }
        }
        if object["ok"] as? Bool == true, let plan = object["data"] as? [String:Any],
           let token = plan["token"] as? String, let summary = plan["summary"] as? String {
            if preparedSummaries.count >= 32 { preparedSummaries.removeAll() }
            preparedSummaries[token] = summary
        }
        if let result=object["data"] as? [String:Any],let csv=result["csv"] as? String,object["ok"] as? Bool == true {
            let panel=NSSavePanel();panel.nameFieldStringValue="跟单信号.csv";panel.allowedContentTypes=[.commaSeparatedText]
            panel.beginSheetModal(for:window){ response in
                if response == .OK,let url=panel.url {
                    do { try ("\u{feff}"+csv).write(to:url,atomically:true,encoding:.utf8);self.respond(["id":id,"ok":true,"data":["saved":true]]) }
                    catch { self.respond(["id":id,"ok":false,"error":"导出失败，请选择可写位置。"])}
                } else { self.respond(["id":id,"ok":true,"data":["saved":false]]) }
            };return
        }
        respond(object)
    }

    func userContentController(_ userContentController:WKUserContentController,didReceive message:WKScriptMessage) {
        guard message.frameInfo.isMainFrame,let url=message.frameInfo.request.url,url.isFileURL, url.lastPathComponent == "api.html",
              var req=message.body as? [String:Any],let action=req["action"] as? String,let id=req["id"] as? Int else{return}
        if let problem=startupError { respond(["id":id,"ok":false,"error":problem]);return }
        if ["api_account_enroll", "api_account_select"].contains(action) {
            forwardAccountChange(id: id, action: action, request: req)
            return
        }
        if !accountChanges.isEmpty && ["api_execute", "api_follow_arm", "api_mfa_setup", "api_review_unknown", "copy_address"].contains(action) {
            respond(["id":id,"ok":false,"error":"正在切换账户，请等待连接完成。"]); return
        }
        if action == "copy_address" {
            guard let data = req["data"] as? [String:Any], let address = data["address"] as? String,
                  address.count <= 200, !address.contains(where: { $0.isWhitespace }) else {
                respond(["id":id,"ok":false,"error":"充值地址无效。"]); return
            }
            NSPasteboard.general.clearContents()
            let copied = NSPasteboard.general.setString(address, forType: .string)
            respond(copied ? ["id":id,"ok":true,"data":["copied":true]] :
                ["id":id,"ok":false,"error":"无法写入剪贴板。"]); return
        }
        if ["api_execute", "api_follow_arm", "api_mfa_setup", "api_review_unknown"].contains(action) {
            guard !confirmationOpen, var data = req["data"] as? [String:Any] else {
                respond(["id":id,"ok":false,"error":"请先完成当前确认。"]); return
            }
            data.removeValue(forKey: "native_authorized")
            let alert = NSAlert()
            alert.alertStyle = .warning
            alert.addButton(withTitle: "取消")
            switch action {
            case "api_execute":
                guard let token = data["token"] as? String, let summary = preparedSummaries.removeValue(forKey: token) else {
                    respond(["id":id,"ok":false,"error":"请重新准备后确认。"]); return
                }
                alert.messageText = "确认 ForeGate 账户操作"
                alert.informativeText = summary
                alert.addButton(withTitle: "确认提交")
            case "api_follow_arm":
                guard let summary = data["summary"] as? String else { return }
                alert.messageText = "启用实盘自动跟单？"
                alert.informativeText = summary
                alert.addButton(withTitle: "启用实盘跟单")
            case "api_mfa_setup":
                alert.messageText = "为 API 账户绑定验证器？"
                alert.informativeText = "接下来将显示验证器密钥，请在你的验证器中录入后输入一次验证码。"
                alert.addButton(withTitle: "继续绑定")
            default:
                alert.messageText = "记录人工核对结果"
                alert.informativeText = "请依据 ForeGate API 账户的订单、成交或资金流水选择结果。\n\n"
                    + (data["label"] as? String ?? "")
                alert.addButton(withTitle: "查到该操作")
                alert.addButton(withTitle: "确认该操作未执行")
            }
            confirmationOpen = true
            let selection = alert.runModal()
            confirmationOpen = false
            guard selection == .alertSecondButtonReturn || (action == "api_review_unknown" && selection == .alertThirdButtonReturn) else {
                respond(["id":id,"ok":false,"error":"操作已取消。"]); return
            }
            if action == "api_review_unknown" {
                data["outcome"] = selection == .alertThirdButtonReturn ? "absent" : "found"
            }
            data["native_authorized"] = true
            req["data"] = data
        }
        if action=="save_settings",var data=req["data"] as? [String:Any] {
            let hash=data.removeValue(forKey:"api_hash") as? String ?? ""
            req["data"]=data
            if !hash.isEmpty {
                guard hash.count==32,hash.allSatisfy({$0.isHexDigit}) else {respond(["id":id,"ok":false,"error":"API Hash 应为 32 位十六进制字符。"]);return}
                secretWrites[id]=hash
            }
        }
        write(req)
    }

    func forwardAccountChange(id: Int, action: String, request: [String:Any]) {
        guard accountChanges.isEmpty, !confirmationOpen, var data = request["data"] as? [String:Any] else {
            respond(["id":id,"ok":false,"error":"请先完成当前账户操作。"]); return
        }
        if action == "api_account_enroll" {
            let keys = ["appKey", "appSecret", "apiKey"]
            var credentials = [String:String]()
            for key in keys {
                guard let raw = data.removeValue(forKey: key) as? String else {
                    respond(["id":id,"ok":false,"error":"请填写完整的 ForeGate OpenAPI 凭证。"]); return
                }
                let value = raw.trimmingCharacters(in: .whitespacesAndNewlines)
                guard !value.isEmpty, value.utf8.count <= 1024,
                      value.utf8.allSatisfy({ (33...126).contains($0) }) else {
                    respond(["id":id,"ok":false,"error":"请填写有效的 ForeGate OpenAPI 凭证。"]); return
                }
                credentials[key] = value
            }
            let slot = UUID().uuidString.lowercased().replacingOccurrences(of: "-", with: "")
            guard let encoded = try? JSONSerialization.data(withJSONObject: credentials),
                  let secret = String(data: encoded, encoding: .utf8),
                  storeSecret(secret, account: "foregate-" + slot) else {
                respond(["id":id,"ok":false,"error":"凭证未能保存到本机钥匙串，请检查钥匙串权限。"]); return
            }
            accountWrites[id] = slot
            data["slot"] = slot
            data["credentials"] = credentials
        } else {
            guard let slot = data["slot"] as? String, slot.count == 32,
                  slot.utf8.allSatisfy({ (48...57).contains($0) || (97...102).contains($0) }),
                  let credentials = (try? JSONSerialization.jsonObject(with:
                    Data(readSecret("foregate-" + slot).utf8))) as? [String:String] else {
                respond(["id":id,"ok":false,"error":"无法读取账户凭证。请使用「连接已有账户」重新填写凭证。"]); return
            }
            data["credentials"] = credentials
        }
        accountChanges.insert(id)
        write(["id":id,"action":action,"data":data])
    }

    func webView(_ webView:WKWebView,didFinish navigation:WKNavigation!) {
        pageReady=true
        for script in queuedScripts { web.evaluateJavaScript(script) };queuedScripts=[]
        if let error=startupError {
            let data=try? JSONSerialization.data(withJSONObject:[error]);let json=String(data:data ?? Data("[]".utf8),encoding:.utf8) ?? "[]"
            web.evaluateJavaScript("toast(\(json)[0],true)")
        }
    }

    func webView(_ webView:WKWebView,decidePolicyFor navigationAction:WKNavigationAction,decisionHandler:@escaping(WKNavigationActionPolicy)->Void) {
        guard let url=navigationAction.request.url else{decisionHandler(.cancel);return}
        if url.isFileURL {decisionHandler(.allow);return}
        if url.scheme=="https",["my.telegram.org","docs.forepaas.org"].contains(url.host ?? "") {NSWorkspace.shared.open(url)}
        decisionHandler(.cancel)
    }

    func applicationShouldTerminate(_ sender:NSApplication) -> NSApplication.TerminateReply {
        if stopping { return .terminateNow }
        stopping=true
        write(["id":-3,"action":"shutdown","data":[:]])
        try? input?.close()
        DispatchQueue.main.asyncAfter(deadline:.now()+2) {
            if let p=self.child,p.isRunning { p.terminate() }
            sender.reply(toApplicationShouldTerminate:true)
        }
        return .terminateLater
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender:NSApplication)->Bool {true}
    func applicationWillTerminate(_ notification:Notification) {
        stopping=true
        write(["id":-3,"action":"shutdown","data":[:]])
        // Closing stdin also causes orderly disconnect if an action is already finishing.
        try? input?.close()
        if let p=child,p.isRunning {
            DispatchQueue.global().asyncAfter(deadline:.now()+3) { if p.isRunning {p.terminate()} }
        }
    }
}
let app=NSApplication.shared
app.setActivationPolicy(.regular)
let delegate=Client();app.delegate=delegate;app.run()

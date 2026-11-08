import AppKit

// メニューバー常駐アプリとして起動する（Dock アイコンは出さない。
// Info.plist の LSUIElement も併せて有効）。
let delegate = AppDelegate()
let app = NSApplication.shared
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()

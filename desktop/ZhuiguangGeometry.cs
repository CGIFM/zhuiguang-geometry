// 追光几何 桌面客户端：把内网服务包进一个真正的窗口，双击就开，不弹命令行、不走浏览器。
// 编译（.NET Framework 4.x，csc 自带）见 desktop/build.ps1。
using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Threading;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Wpf;

[assembly: System.Reflection.AssemblyTitle("追光几何")]
[assembly: System.Reflection.AssemblyProduct("追光几何")]
[assembly: System.Reflection.AssemblyVersion("1.0.0.0")]

static class Program
{
    static Mutex _one;
    static string _base;
    static string _script;
    static int _port = 8795;
    static int _debugPort;
    static bool _silent;
    static EventWaitHandle _showEvent;

    /// <summary>这个目录是不是装好的一份追光几何（根目录或 app\ 子目录里有 zgr.py）。</summary>
    static bool LooksLikeApp(string dir)
    {
        return File.Exists(Path.Combine(dir, "zgr.py")) ||
               File.Exists(Path.Combine(dir, @"app\zgr.py"));
    }

    /// <summary>
    /// 两种部署布局都要认：
    ///   安装版  {app}\zgr.py            + {app}\desktop\追光几何.exe
    ///   绿色版  {base}\app\zgr.py       + {base}\desktop\追光几何.exe
    /// </summary>
    static string FindScript(string start)
    {
        string d = start;
        for (int i = 0; i < 3 && d != null; i++)
        {
            if (LooksLikeApp(d))
            {
                string flat = Path.Combine(d, "zgr.py");
                return File.Exists(flat) ? flat : Path.Combine(d, @"app\zgr.py");
            }
            DirectoryInfo up = Directory.GetParent(d);
            d = up == null ? null : up.FullName.TrimEnd('\\');
        }
        return null;
    }

    [STAThread]
    static void Main(string[] args)
    {
        _base = AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\');
        _script = FindScript(_base);
        if (_script != null) _base = Path.GetDirectoryName(_script.TrimEnd('\\')).TrimEnd('\\');
        if (_base.EndsWith(@"\app", StringComparison.OrdinalIgnoreCase))
            _base = Path.GetDirectoryName(_base).TrimEnd('\\');
        for (int i = 0; i < args.Length; i++)
        {
            if (args[i] == "--port" && i + 1 < args.Length) int.TryParse(args[i + 1], out _port);
            if (args[i] == "--debug-port" && i + 1 < args.Length) int.TryParse(args[i + 1], out _debugPort);
            // 开机自启用：只起服务 + 托盘图标，不弹窗口
            if (args[i] == "--silent" || args[i] == "--tray") _silent = true;
        }

        bool created;
        _one = new Mutex(true, "ZhuiguangGeometry.Desktop." + _port, out created);
        if (!created)
        {
            // 已经有一个实例在跑（比如开机自启的托盘实例）：把它叫醒，而不是什么都不发生
            try { EventWaitHandle.OpenExisting("ZhuiguangGeometry.Show." + _port).Set(); }
            catch { }
            return;
        }
        try { _showEvent = new EventWaitHandle(false, EventResetMode.AutoReset, "ZhuiguangGeometry.Show." + _port); }
        catch { }

        var app = new ZgrApp();
        app.InitializeComponent();
        app.Run();
    }

    public static string BaseDir { get { return _base; } }
    public static string Script { get { return _script; } }
    public static int Port { get { return _port; } }
    public static string Url { get { return "http://127.0.0.1:" + _port + "/"; } }
    public static int DebugPort { get { return _debugPort; } }
    public static bool Silent { get { return _silent; } }
    public static EventWaitHandle ShowEvent { get { return _showEvent; } }

    public static bool Ping()
    {
        try
        {
            var req = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:" + _port + "/api/ping");
            req.Proxy = null;
            req.Timeout = 1200;
            req.ReadWriteTimeout = 1200;
            using (var res = (HttpWebResponse)req.GetResponse())
            {
                return res.StatusCode == HttpStatusCode.OK;
            }
        }
        catch { return false; }
    }

    /// <summary>服务没起来就用 pythonw（无控制台）拉起，最多等 20 秒。</summary>
    public static bool EnsureService()
    {
        if (Ping()) return true;
        string pyw = Path.Combine(_base, @"python\pythonw.exe");
        string py = Path.Combine(_base, @"python\python.exe");
        string exe = File.Exists(pyw) ? pyw : py;
        string script = _script;
        if (script == null || !File.Exists(exe) || !File.Exists(script)) return false;
        try
        {
            var psi = new ProcessStartInfo(exe, "\"" + script + "\" serve --port " + _port + " --host 0.0.0.0");
            psi.WorkingDirectory = _base;
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            psi.WindowStyle = ProcessWindowStyle.Hidden;
            Process.Start(psi);
        }
        catch { return false; }
        for (int i = 0; i < 100; i++)
        {
            Thread.Sleep(200);
            if (Ping()) return true;
        }
        return Ping();
    }
}

class ZgrApp : System.Windows.Application
{
    System.Windows.Window _win;
    WebView2 _web;
    NotifyIcon _tray;
    bool _reallyExit;
    bool _toldTray;

    public void InitializeComponent()
    {
        ShutdownMode = ShutdownMode.OnExplicitShutdown;
        DispatcherUnhandledException += (s, e) => { e.Handled = true; };

        if (!Program.EnsureService())
        {
            System.Windows.MessageBox.Show(
                "本地服务没起来：\n" + Program.BaseDir +
                "\n\n请确认这个目录里有 python\\pythonw.exe 和 zgr.py（或 app\\zgr.py），" +
                "然后重新打开。",
                "追光几何", MessageBoxButton.OK, MessageBoxImage.Warning);
            Shutdown();
            return;
        }

        _win = new System.Windows.Window();
        _win.Title = "追光几何";
        _win.Width = 1440;
        _win.Height = 920;
        _win.MinWidth = 900;
        _win.MinHeight = 600;
        _win.WindowStartupLocation = WindowStartupLocation.CenterScreen;
        _win.Background = System.Windows.Media.Brushes.White;
        LoadIcon();

        var root = new Grid();
        _win.Content = root;

        _web = new WebView2();
        var props = new Microsoft.Web.WebView2.Wpf.CoreWebView2CreationProperties
        {
            UserDataFolder = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "ZhuiguangGeometry", "WebView2"),
        };
        // 只在带 --debug-port 启动时开放 CDP（本机 127.0.0.1），正常双击不会开
        if (Program.DebugPort > 0)
            props.AdditionalBrowserArguments =
                "--remote-debugging-port=" + Program.DebugPort + " --remote-allow-origins=*";
        _web.CreationProperties = props;
        root.Children.Add(_web);

        _web.CoreWebView2InitializationCompleted += OnWebReady;
        _win.Closing += OnClosing;
        _win.Closed += (s, e) => { };
        BuildTray();
        ListenForShow();

        try { _web.Source = new Uri(Program.Url); }
        catch { FallbackBrowser(); return; }

        // --silent（开机自启）：只保留服务和托盘图标，不弹窗口抢屏幕
        if (!Program.Silent) _win.Show();
    }

    void LoadIcon()
    {
        try
        {
            string ico = Path.Combine(Program.BaseDir, @"assets\zgr.ico");
            if (File.Exists(ico))
            {
                _win.Icon = System.Windows.Media.Imaging.BitmapFrame.Create(
                    new Uri(ico), System.Windows.Media.Imaging.BitmapCreateOptions.None,
                    System.Windows.Media.Imaging.BitmapCacheOption.OnLoad);
            }
        }
        catch { }
    }

    void OnWebReady(object sender, Microsoft.Web.WebView2.Core.CoreWebView2InitializationCompletedEventArgs e)
    {
        if (!e.IsSuccess)
        {
            FallbackBrowser();
            return;
        }
        var core = _web.CoreWebView2;
        // 像原生软件：不要网页右键菜单、不要 Ctrl+P / Ctrl+滚轮缩放 / F5 白屏刷新。
        // 调试仍可通过 --debug-port 的 CDP 端口进行，不受影响。
        core.Settings.AreDevToolsEnabled = Program.DebugPort > 0;
        core.Settings.IsStatusBarEnabled = false;
        core.Settings.AreDefaultContextMenusEnabled = false;
        core.Settings.IsZoomControlEnabled = false;
        core.Settings.AreBrowserAcceleratorKeysEnabled = Program.DebugPort > 0;
        // 标题跟着当前项目走：任务栏/Alt-Tab 里看到的是「项目名 · 追光几何」
        core.DocumentTitleChanged += (s, a) =>
        {
            try
            {
                string t = core.DocumentTitle;
                _win.Title = string.IsNullOrWhiteSpace(t) ? "追光几何" : t;
            }
            catch { }
        };
        core.NewWindowRequested += (s, a) =>
        {
            a.Handled = true;   // 网页里的外链不弹新窗口，统一在应用里开
            try { core.Navigate(a.Uri); } catch { }
        };
        // F5 刷新、Ctrl+Shift+I 开发者工具
        _win.KeyDown += (s, k) =>
        {
            if (k.Key == System.Windows.Input.Key.F5) { core.Reload(); k.Handled = true; }
            if (k.Key == System.Windows.Input.Key.I &&
                (System.Windows.Input.Keyboard.Modifiers & System.Windows.Input.ModifierKeys.Control) != 0 &&
                (System.Windows.Input.Keyboard.Modifiers & System.Windows.Input.ModifierKeys.Shift) != 0)
            {
                core.OpenDevToolsWindow();
                k.Handled = true;
            }
        };
    }

    static string FindEdge()
    {
        string[] c =
        {
            @"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            @"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        };
        foreach (var p in c) if (File.Exists(p)) return p;
        return null;
    }

    /// <summary>WebView2 运行时缺失或太旧时，退回到 Edge 的独立应用窗口（仍然没有地址栏/标签页）。</summary>
    void FallbackBrowser()
    {
        string edge = FindEdge();
        if (edge == null)
        {
            System.Windows.MessageBox.Show("没有找到 WebView2 运行时，也没有找到 Edge，无法打开界面。",
                "追光几何", MessageBoxButton.OK, MessageBoxImage.Error);
            Shutdown();
            return;
        }
        try
        {
            var psi = new ProcessStartInfo(edge,
                "--app=" + Program.Url + " --window-size=1440,920" +
                " --user-data-dir=\"" + Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                    "ZhuiguangGeometry", "EdgeApp") + "\"");
            psi.UseShellExecute = false;
            Process.Start(psi);
        }
        catch
        {
            System.Windows.MessageBox.Show("界面启动失败，请手动打开 " + Program.Url,
                "追光几何", MessageBoxButton.OK, MessageBoxImage.Error);
        }
        Shutdown();
    }

    void BuildTray()
    {
        try
        {
            _tray = new NotifyIcon();
            string ico = Path.Combine(Program.BaseDir, @"assets\zgr.ico");
            _tray.Icon = File.Exists(ico) ? new Icon(ico) : SystemIcons.Application;
            _tray.Text = "追光几何（同步在后台运行）";
            _tray.Visible = true;
            var menu = new ContextMenuStrip();
            menu.Items.Add("打开主界面", null, (s, e) => ShowMain());
            menu.Items.Add(new ToolStripSeparator());
            menu.Items.Add("退出窗口（同步继续在后台）", null, (s, e) => ExitApp());
            _tray.ContextMenuStrip = menu;
            _tray.DoubleClick += (s, e) => ShowMain();
        }
        catch { _tray = null; }
    }

    /// <summary>别的实例（或桌面上再双击一次）敲这个事件，就把主窗口叫到前台。</summary>
    void ListenForShow()
    {
        var ev = Program.ShowEvent;
        if (ev == null) return;
        var t = new Thread(() =>
        {
            while (true)
            {
                try { ev.WaitOne(); }
                catch { return; }
                try { Dispatcher.BeginInvoke(new Action(ShowMain)); }
                catch { return; }
            }
        });
        t.IsBackground = true;
        t.Start();
    }

    void ShowMain()
    {
        if (_win == null) return;
        _win.Show();
        if (_win.WindowState == WindowState.Minimized) _win.WindowState = WindowState.Normal;
        _win.Activate();
    }

    void OnClosing(object sender, System.ComponentModel.CancelEventArgs e)
    {
        if (_reallyExit) return;
        e.Cancel = true;            // 点 × 只是收起窗口，服务继续在后台同步
        _win.Hide();
        if (!_toldTray && _tray != null)
        {
            _toldTray = true;
            try
            {
                _tray.ShowBalloonTip(2500, "追光几何还在后台",
                    "窗口已收到右下角托盘，实时同步继续运行。双击托盘图标可以重新打开。",
                    ToolTipIcon.Info);
            }
            catch { }
        }
    }

    void ExitApp()
    {
        _reallyExit = true;
        try { if (_web != null) _web.Dispose(); } catch { }
        if (_tray != null) { _tray.Visible = false; _tray.Dispose(); }
        Shutdown();
    }
}

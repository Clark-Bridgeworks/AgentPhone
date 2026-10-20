using System;
using System.IO;
using System.Text;
using System.Threading;
using System.Collections.Generic;
using System.Web.Script.Serialization;
using System.Windows.Automation;
using System.Runtime.InteropServices;

// Only selects the API-verified original conversation in the installed DSH window.
// Message transport and session identity are handled by DSH's authenticated API.
class DshWindow
{
    [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr window);
    [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr window, int command);

    static AutomationElement MainWindow(int pid)
    {
        var windows = AutomationElement.RootElement.FindAll(TreeScope.Children,
            new AndCondition(new PropertyCondition(AutomationElement.ProcessIdProperty, pid),
                new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Window)));
        AutomationElement match = null;
        foreach (AutomationElement window in windows)
        {
            if (!window.Current.Name.EndsWith("DeepSeek Harness", StringComparison.Ordinal)) continue;
            if (match != null) throw new Exception("DSH has more than one main window.");
            match = window;
        }
        return match;
    }

    static AutomationElement SessionItem(AutomationElement window, string title)
    {
        AutomationElement found = null;
        var items = window.FindAll(TreeScope.Descendants,
            new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.TreeItem));
        foreach (AutomationElement item in items)
        {
            string name = item.Current.Name;
            if (name != title && !name.StartsWith(title + " ", StringComparison.Ordinal)
                && name.IndexOf(" " + title + " ", StringComparison.Ordinal) < 0) continue;
            if (found != null) throw new Exception("DSH session title is ambiguous.");
            found = item;
        }
        return found;
    }

    static void Select(int pid, string title)
    {
        DateTime deadline = DateTime.UtcNow.AddSeconds(12);
        while (DateTime.UtcNow < deadline)
        {
            var window = MainWindow(pid);
            if (window == null) { Thread.Sleep(250); continue; }
            IntPtr handle = new IntPtr(window.Current.NativeWindowHandle);
            ShowWindow(handle, 9);
            SetForegroundWindow(handle);
            if (window.Current.Name.IndexOf(title, StringComparison.Ordinal) >= 0) return;
            var item = SessionItem(window, title);
            if (item == null) { Thread.Sleep(250); continue; }
            object pattern;
            if (item.TryGetCurrentPattern(SelectionItemPattern.Pattern, out pattern))
                ((SelectionItemPattern)pattern).Select();
            else if (item.TryGetCurrentPattern(InvokePattern.Pattern, out pattern))
                ((InvokePattern)pattern).Invoke();
            else throw new Exception("DSH session does not expose a selection action.");
            DateTime selectedDeadline = DateTime.UtcNow.AddSeconds(3);
            while (DateTime.UtcNow < selectedDeadline)
            {
                if (window.Current.Name.IndexOf(title, StringComparison.Ordinal) >= 0) return;
                Thread.Sleep(150);
            }
            throw new Exception("DSH did not display the selected bridge conversation.");
        }
        throw new Exception("The bridge conversation was not found in the DSH sidebar.");
    }

    [STAThread]
    static int Main(string[] args)
    {
        if (args.Length != 2) return 2;
        var json = new JavaScriptSerializer();
        var result = new Dictionary<string, object>();
        try
        {
            var input = json.Deserialize<Dictionary<string, object>>(File.ReadAllText(args[0], Encoding.UTF8));
            int pid = Convert.ToInt32(input["pid"]);
            string title = Convert.ToString(input["title"]);
            string sessionId = Convert.ToString(input["native_session_id"]);
            if (pid <= 0 || String.IsNullOrWhiteSpace(title) || title.Length > 512
                || !sessionId.StartsWith("session-", StringComparison.Ordinal))
                throw new Exception("Invalid native conversation window target.");
            Select(pid, title);
            result["selected"] = true;
            result["pid"] = pid;
            result["title"] = title;
        }
        catch (Exception error)
        {
            result["selected"] = false;
            result["error"] = error.Message;
        }
        File.WriteAllText(args[1], json.Serialize(result), new UTF8Encoding(false));
        return (bool)result["selected"] ? 0 : 1;
    }
}

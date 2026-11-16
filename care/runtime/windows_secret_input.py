"""Masked native Windows input; credentials never enter the WebView."""

import threading


class WindowsNativeSecretCapture:
    def __init__(self, *, timeout_seconds: float = 180) -> None:
        self._timeout = timeout_seconds

    def __call__(self, request) -> str | None:
        # Reuse pywebview's loaded WinForms/pythonnet backend. A dedicated STA
        # thread owns the dialog, independent of the JavaScript bridge worker.
        from webview.platforms import winforms as native
        from care.harness_settings import MAX_NATIVE_SECRET_CHARS

        completed = threading.Event()
        result = {}
        state = {}

        def present():
            form = native.WinForms.Form()
            field = native.WinForms.TextBox()
            try:
                form.Text = request.title
                form.ClientSize = native.Size(500, 180)
                form.FormBorderStyle = native.WinForms.FormBorderStyle.FixedDialog
                form.StartPosition = native.WinForms.FormStartPosition.CenterScreen
                form.MaximizeBox = form.MinimizeBox = False
                form.TopMost = True
                label = native.WinForms.Label()
                label.Text = request.label + "\n" + request.message
                label.Location = native.Point(16, 12)
                label.Size = native.Size(468, 62)
                field.Location = native.Point(16, 80)
                field.Size = native.Size(468, 24)
                field.UseSystemPasswordChar = True
                field.MaxLength = MAX_NATIVE_SECRET_CHARS
                accept = native.WinForms.Button()
                accept.Text = "Use key"
                accept.Location = native.Point(282, 130)
                accept.Size = native.Size(95, 30)
                accept.DialogResult = native.WinForms.DialogResult.OK
                cancel = native.WinForms.Button()
                cancel.Text = "Cancel"
                cancel.Location = native.Point(389, 130)
                cancel.Size = native.Size(95, 30)
                cancel.DialogResult = native.WinForms.DialogResult.Cancel
                for control in (label, field, accept, cancel):
                    form.Controls.Add(control)
                form.AcceptButton = accept
                form.CancelButton = cancel
                form.Shown += lambda *_: field.Focus()
                state["form"] = form
                if form.ShowDialog() == native.WinForms.DialogResult.OK:
                    result["value"] = str(field.Text)
            except Exception as exc:
                result["error"] = exc
            finally:
                field.Text = ""
                form.Dispose()
                completed.set()

        thread = native.Thread(native.ThreadStart(present))
        thread.SetApartmentState(native.ApartmentState.STA)
        thread.IsBackground = True
        thread.Start()
        if not completed.wait(self._timeout):
            form = state.get("form")
            if form is not None:
                form.BeginInvoke(native.Func[native.Type](lambda: form.Close()))
            raise RuntimeError("native secure input timed out")
        if result.get("error"):
            raise RuntimeError("native secure input failed") from result["error"]
        return result.get("value")

import pathlib

p = pathlib.Path("app/ui/main_window.py")
s = p.read_text(encoding="utf-8")
n = s.count("self.statusBar().showMessage(")
s = s.replace("self.statusBar().showMessage(", "self.status_bar.set_message(")
p.write_text(s, encoding="utf-8")
print("replaced:", n)

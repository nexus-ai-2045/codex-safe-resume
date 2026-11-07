"""利用枠の表示専用Tk部品。取得・設定変更・送信は行わない。"""
from tkinter import ttk


class QuotaCards(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, style='Main.TFrame')
        self.items = []
        self.keys = None
        self.columns = None
        self.bind('<Configure>', self._resize, add='+')

    def _resize(self, event):
        if event.widget is self:
            self._layout(event.width)

    def _layout(self, width):
        columns = 2 if width >= 640 else 1
        if self.columns == columns:
            return
        self.columns = columns
        self.columnconfigure(0, weight=1, uniform='quota' if columns == 2 else '')
        self.columnconfigure(1, weight=1 if columns == 2 else 0,
                             uniform='quota' if columns == 2 else '')
        for index, item in enumerate(self.items):
            item['frame'].grid(row=index // columns, column=index % columns,
                               sticky='nsew', padx=(0, 6) if columns == 2 and index % 2 == 0 else 0,
                               pady=(0, 4))

    def render(self, view):
        cards = view['cards']
        keys = tuple(card['key'] for card in cards)
        if self.keys != keys:
            for item in self.items:
                item['frame'].destroy()
            self.items = []
            for card in cards:
                frame = ttk.Frame(self, style='Panel.TFrame', padding=(10, 5))
                frame.columnconfigure(0, weight=1)
                labels = []
                for row in range(3):
                    label = ttk.Label(frame, style='Panel.TLabel', justify='left', wraplength=280)
                    label.grid(row=row if row == 0 else row + 1, column=0, sticky='ew')
                    label.bind('<Configure>', self._wrap_label)
                    labels.append(label)
                gauge = ttk.Progressbar(frame, mode='determinate', maximum=100, length=80)
                gauge.grid(row=1, column=0, sticky='ew', pady=3)
                self.items.append(dict(frame=frame, labels=labels, gauge=gauge))
            self.keys, self.columns = keys, None
            self._layout(self.winfo_width())
        for item, card in zip(self.items, cards):
            heading, summary, reset = item['labels']
            heading.configure(text=card['name'])
            summary.configure(text=card['summary'])
            prefix = '前回取得値・更新待ち / ' if view['stale'] else ''
            reset.configure(text=prefix + card['reset_text'])
            gauge = item['gauge']
            if card['remaining_percent'] is None:
                gauge.grid_remove()
            else:
                gauge.configure(value=card['remaining_percent'])
                gauge.grid()

    def _wrap_label(self, event):
        if event.width > 1:
            width = max(1, event.width - 4)
            if int(event.widget.cget('wraplength')) != width:
                event.widget.configure(wraplength=width)

import time
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler


# 1. 自定义事件处理器
class MyHandler(FileSystemEventHandler):
    # 当文件或文件夹被创建时触发
    def on_created(self, event):
        print(f"[创建] {event.src_path}")

    # 当文件或文件夹被修改时触发
    def on_modified(self, event):
        print(f"[修改] {event.src_path}")

    # 当文件或文件夹被删除时触发
    def on_deleted(self, event):
        print(f"[删除] {event.src_path}")

    # 当文件或文件夹被移动/重命名时触发
    def on_moved(self, event):
        # 对于移动事件，src_path是原路径，dest_path是新路径
        print(f'[移动] 从 "{event.src_path}" 到 "{event.dest_path}"')


if __name__ == "__main__":
    # 2. 设置要监控的目录路径
    path_to_watch = "."  # 监控当前目录，也可以写成 "/your/path"

    # 3. 创建事件处理器和观察者
    event_handler = MyHandler()
    observer = Observer()

    # 4. 将观察者与处理器绑定，并开始监控
    # recursive=True 表示监控所有子目录
    observer.schedule(event_handler, path_to_watch, recursive=True)
    observer.start()

    print(f"正在监控目录: {path_to_watch}，按 Ctrl+C 停止...")

    try:
        # 5. 保持主程序运行
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        # 6. 用户按下 Ctrl+C 后，优雅地停止监控
        observer.stop()
        print("\n监控已停止。")

    observer.join()

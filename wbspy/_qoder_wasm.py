"""qoder-auth-wasm.wasm 的 Python 运行时（wasmtime）。

Qoder 的加密推理请求需要官方 wasm（qoder-auth-wasm）生成
``Bearer COSY.<载荷>.<签名>`` 并加密请求体。本模块用 wasmtime 加载
wasm，并复刻 dsh-xdpool vendor/channel-pack 自写的精简 glue（非官方
wasm-bindgen glue，imports 共 31 个，2026-10 实测版本）：

* 对象表（wasm-bindgen 风格 1024 槽 + undefined/null/true/false 前置）
* 内存读写（__wbindgen_export2 分配 / export4 释放）
* getRandomValues（os.urandom 直写 wasm 内存 / shim 对象双签名）
* Map / Date.now / Error 构造等最小 shim

暴露三个高层操作（与 pack.js 语义逐一对齐）：

* ``generate_runtime_auth_fields(user_payload) -> dict``
  （``{encrypt_user_info, key}``）
* ``QoderContext(machine_id, version, user_info, metadata)``
* ``QoderContext.prepare_infer_request(host, body_json, model_key, source)
  -> {"url", "headers", "body"}``

⚠️ 返回的 headers 必须原样透传：Authorization 是 wasm 生成的
``Bearer COSY.<载荷>.<签名>``，用普通 ``Bearer <token>`` 覆盖会导致
403 Signature invalid。
"""

from __future__ import annotations

import json
import os
import struct
import threading
import time
from pathlib import Path

WASM_PATH = Path(__file__).parent / "data" / "qoder-auth-wasm.wasm"
IMPORT_MODULE = "./qoder_auth_wasm_bg.js"   # wasm-bindgen 默认模块名

_UNDEF = object()          # objects[1024]


class WasmThrow(Exception):
    """__wbindgen_throw / JS Error：wasm 内部错误（含 Rust panic 文本）。"""


class _CryptoShim:
    """globalThis.crypto 的最小 shim：让 Rust getrandom 走 Web 分支。"""

    @staticmethod
    def getRandomValues(view):
        # Web 分支的签名 (ptr, len) 在 import 里直接写 wasm 内存，
        # 不会以对象形式进来；对象形式一律按 bytearray 处理。
        try:
            data = os.urandom(len(view))
            view[:] = data
        except TypeError:                      # pragma: no cover - 防御
            raise WasmThrow("getRandomValues: unsupported target")


class _GlobalShim:
    """globalThis shim：crypto 存在（getrandom 走 Web 分支）、process 等不存在。"""

    crypto = _CryptoShim()

    def __getattr__(self, name):
        return _UNDEF                             # process / window / …


class _ObjTable:
    """wasm-bindgen 对象表：1024 保留槽 + undefined/null/true/false。"""

    def __init__(self):
        self.cells = [None] * 1024 + [_UNDEF, None, True, False]
        self.first_free = 1028

    def get(self, idx):
        return self.cells[idx]

    def push(self, value):
        if self.first_free == len(self.cells):
            # 对齐 JS glue：扩表并把「下一槽号」写进新槽（free 链节点）
            self.cells.append(len(self.cells) + 1)
        idx, self.first_free = self.first_free, self.cells[self.first_free]
        self.cells[idx] = value
        return idx

    def take(self, idx):
        value = self.cells[idx]
        if idx >= 1028:
            self.cells[idx] = self.first_free
            self.first_free = idx
        return value


class QoderWasmError(RuntimeError):
    """wasm 运行时不可用（缺 wasmtime / wasm 损坏 / 实例化失败）。"""


class QoderWasm:
    """wasm 实例 + glue。每个进程一份（线程安全：调用串行锁）。"""

    def __init__(self):
        try:
            from wasmtime import Engine, Func, FuncType, Instance, Linker, Module, Store, ValType
        except ImportError as exc:             # pragma: no cover - 依赖缺失
            raise QoderWasmError(
                "wasmtime 未安装（pip install wasmtime）——Qoder 加密推理不可用。"
            ) from exc
        self._obj = _ObjTable()
        self._last_len = 0
        engine = Engine()
        self._store = Store(engine)
        module = Module.from_file(engine, str(WASM_PATH))
        self._mod = module
        # import 全名 → handler 名（精确映射，杜绝花哨改名出错）
        HANDLERS = {
            "__wbindgen_object_drop_ref": "_imp_object_drop_ref",
            "__wbindgen_object_clone_ref": "_imp_object_clone_ref",
            "__wbindgen_cast_0000000000000001": "_imp_cast_0000000000000001",
            "__wbindgen_cast_0000000000000002": "_imp_cast_0000000000000002",
            "__wbg_set_08463b1df38a7e29": "_imp_set_08463b1df38a7e29",
            "__wbg_getRandomValues_d49329ff89a07af1": "_imp_getRandomValues_d49329ff89a07af1",
            "__wbg_getRandomValues_c44a50d8cfdaebeb": "_imp_getRandomValues_c44a50d8cfdaebeb",
            "__wbg_crypto_38df2bab126b63dc": "_imp_crypto_38df2bab126b63dc",
            "__wbg_process_44c7a14e11e9f69e": "_imp_process_44c7a14e11e9f69e",
            "__wbg_versions_276b2795b1c6a219": "_imp_versions_276b2795b1c6a219",
            "__wbg_node_84ea875411254db1": "_imp_node_84ea875411254db1",
            "__wbg_require_b4edbdcf3e2a1ef0": "_imp_require_b4edbdcf3e2a1ef0",
            "__wbg_msCrypto_bd5a034af96bcba6": "_imp_msCrypto_bd5a034af96bcba6",
            "__wbg_randomFillSync_6c25eac9869eb53c": "_imp_randomFillSync_6c25eac9869eb53c",
            "__wbg_call_d578befcc3145dee": "_imp_call_d578befcc3145dee",
            "__wbg_new_with_length_9cedd08484b73942": "_imp_new_with_length_9cedd08484b73942",
            "__wbg_length_0c32cb8543c8e4c8": "_imp_length_0c32cb8543c8e4c8",
            "__wbg_prototypesetcall_3e05eb9545565046": "_imp_prototypesetcall_3e05eb9545565046",
            "__wbg_subarray_0f98d3fb634508ad": "_imp_subarray_0f98d3fb634508ad",
            "__wbg_new_99cabae501c0a8a0": "_imp_new_99cabae501c0a8a0",
            "__wbg_now_88621c9c9a4f3ffc": "_imp_now_88621c9c9a4f3ffc",
            "__wbg_static_accessor_GLOBAL_THIS_a1248013d790bf5f": "_imp_static_accessor_GLOBAL",
            "__wbg_static_accessor_GLOBAL_f2e0f995a21329ff": "_imp_static_accessor_GLOBAL",
            "__wbg_static_accessor_SELF_24f78b6d23f286ea": "_imp_static_accessor_SELF",
            "__wbg_static_accessor_WINDOW_59fd959c540fe405": "_imp_static_accessor_SELF",
            "__wbg___wbindgen_throw_81fc77679af83bc6": "_imp__wbindgen_throw",
            "__wbg_Error_2e59b1b37a9a34c3": "_imp_Error_2e59b1b37a9a34c3",
            "__wbg___wbindgen_is_object_40c5a80572e8f9d3": "_imp__wbindgen_is_object",
            "__wbg___wbindgen_is_string_b29b5c5a8065ba1a": "_imp__wbindgen_is_string",
            "__wbg___wbindgen_is_function_49868bde5eb1e745": "_imp__wbindgen_is_function",
            "__wbg___wbindgen_is_undefined_c0cca72b82b86f4d": "_imp__wbindgen_is_undefined",
        }

        def dispatch(name, args):
            key = HANDLERS.get(name)
            handler = getattr(self, key, None) if key else None
            if handler is None:                # pragma: no cover - 全表应齐
                raise WasmThrow(f"unimplemented import: {name}")
            return handler(*args)

        linker = Linker(engine)
        for imp in module.imports:
            ftype = imp.type
            params = list(ftype.params)
            n_results = len(ftype.results)
            name = imp.name

            def make_cb(name=name, n_results=n_results):
                def cb(*args):                    # wasmtime 回调不传 store
                    out = dispatch(name, args)
                    if n_results == 0:
                        return None
                    return out if out is not None else 0
                return cb

            linker.define(self._store, IMPORT_MODULE, name,
                          Func(self._store, FuncType(params, list(ftype.results)),
                               make_cb()))

        instance = linker.instantiate(self._store, module)
        self._inst = instance
        ex = instance.exports(self._store)
        self._memory = ex["memory"]
        self._ex = {k: ex[k] for k in
                    ("__wbindgen_export2", "__wbindgen_export4", "__wbindgen_export",
                     "__wbindgen_add_to_stack_pointer",
                     "generate_runtime_auth_fields",
                     "qodercontext_new", "qodercontext_prepareInferRequest",
                     "requestresult_url", "requestresult_headers",
                     "requestresult_body")}
        self._lock = threading.Lock()

    # -- 内存原语 ----------------------------------------------------------

    def _read(self, ptr: int, length: int) -> bytes:
        return bytes(self._memory.read(self._store, ptr, ptr + length))

    def _write(self, ptr: int, data: bytes) -> None:
        self._memory.write(self._store, data, ptr)

    def _i32(self, ptr: int, offset: int = 0) -> int:
        return struct.unpack("<i", self._read(ptr + offset, 4))[0]

    def _read_str(self, ptr: int, length: int) -> str:
        return self._read(ptr, length).decode("utf-8", "replace")

    def write_string(self, text: str) -> int:
        encoded = text.encode("utf-8")
        ptr = self._ex["__wbindgen_export2"](self._store, len(encoded), 1)
        self._write(ptr, encoded)
        self._last_len = len(encoded)
        return ptr

    def _free(self, ptr: int, length: int) -> None:
        self._ex["__wbindgen_export4"](self._store, ptr, length, 1)

    def _stack_adjust(self, delta: int) -> int:
        return self._ex["__wbindgen_add_to_stack_pointer"](self._store, delta)

    # -- glue imports（对齐 pack.js createGlue，名字去掉前缀后注册） --------

    def _imp_object_drop_ref(self, a):
        self._obj.take(a)

    def _imp_object_clone_ref(self, a):
        return self._obj.push(self._obj.get(a))

    def _imp_cast_0000000000000001(self, a, e):
        return self._obj.push(bytearray(self._read(a, e)))

    def _imp_cast_0000000000000002(self, a, e):
        return self._obj.push(self._read_str(a, e))

    def _imp_set_08463b1df38a7e29(self, a, e, t):
        self._obj.get(a)[self._obj.get(e)] = self._obj.get(t)

    def _imp_getRandomValues_d49329ff89a07af1(self, ptr, length):
        self._write(ptr, os.urandom(length))

    def _imp_getRandomValues_c44a50d8cfdaebeb(self, a, e):
        # JS glue 两个参数都是 getObject（get 非 take）——take 会把
        # 缓冲区对象放回 free 链，后续 length 调用就拿到 int 了
        self._obj.get(a).getRandomValues(self._obj.get(e))

    def _imp_crypto_38df2bab126b63dc(self, a):
        return self._obj.push(self._obj.get(a).crypto)

    def _imp_process_44c7a14e11e9f69e(self, a):
        v = self._obj.get(a).process
        return self._obj.push(_UNDEF if v is None or v is _UNDEF else v)

    def _imp_versions_276b2795b1c6a219(self, a):
        v = getattr(self._obj.get(a), "versions", _UNDEF)
        return self._obj.push(_UNDEF if v is None or v is _UNDEF else v)

    def _imp_node_84ea875411254db1(self, a):
        v = getattr(self._obj.get(a), "node", _UNDEF)
        return self._obj.push(_UNDEF if v is None or v is _UNDEF else v)

    def _imp_require_b4edbdcf3e2a1ef0(self, *args):
        raise WasmThrow("node:crypto require 不可用（Python 运行时走 Web 分支）")

    def _imp_msCrypto_bd5a034af96bcba6(self, a):
        return self._obj.push(_UNDEF)

    def _imp_randomFillSync_6c25eac9869eb53c(self, *args):
        raise WasmThrow("randomFillSync 不可用（Python 运行时走 Web 分支）")

    def _imp_call_d578befcc3145dee(self, fn, self_, arg):
        target = self._obj.get(fn)
        return self._obj.push(target.call(self._obj.get(self_),
                                          self._obj.get(arg)))

    def _imp_new_with_length_9cedd08484b73942(self, n):
        return self._obj.push(bytearray(n))

    def _imp_length_0c32cb8543c8e4c8(self, a):
        return len(self._obj.get(a))

    def _imp_prototypesetcall_3e05eb9545565046(self, a, e, t):
        src = bytes(self._obj.get(t))
        self._write(a, src[:e])

    def _imp_subarray_0f98d3fb634508ad(self, a, e, t):
        # JS: getObject(a).subarray(e, t)——对 shim 对象取切片
        return self._obj.push(self._obj.get(a)[e:t])

    def _imp_new_99cabae501c0a8a0(self):
        return self._obj.push({})

    def _imp_now_88621c9c9a4f3ffc(self):
        return float(time.time() * 1000)          # Date.now() → f64

    def _imp_static_accessor_GLOBAL(self):
        return self._obj.push(_GlobalShim())

    def _imp_static_accessor_SELF(self):
        return 0                                  # JS undefined → 0

    def _imp_static_accessor_WINDOW(self):
        return 0

    def _imp__wbindgen_throw(self, p, l):
        raise WasmThrow(self._read_str(p, l))

    def _imp_Error_2e59b1b37a9a34c3(self, p, l):
        return self._obj.push(WasmThrow(self._read_str(p, l)))

    def _imp__wbindgen_is_object(self, idx):
        # shim 家族（globalThis/crypto/Map/bytearray）都算 JS 对象；
        # _UNDEF/None/str/int 等基元不算——Rust getrandom 靠这个探测
        # crypto 对象，误判会走 node fallback 并 panic unreachable。
        v = self._obj.get(idx)
        return int(isinstance(v, (dict, list, _GlobalShim, _CryptoShim)))

    def _imp__wbindgen_is_string(self, idx):
        return int(isinstance(self._obj.get(idx), str))

    def _imp__wbindgen_is_function(self, idx):
        return int(callable(self._obj.get(idx)))

    def _imp__wbindgen_is_undefined(self, idx):
        return int(self._obj.get(idx) is _UNDEF)

    # -- 高层操作 ----------------------------------------------------------

    def _call_string(self, invoke) -> str:
        """callString 模式：结果 (ptr, len, obj, isError) 写在栈上。"""
        stack = self._stack_adjust(-16)
        try:
            invoke(stack)
            ptr, length = self._i32(stack), self._i32(stack, 4)
            obj, is_err = self._i32(stack, 8), self._i32(stack, 12)
            if is_err:
                raise self._obj.take(obj)
            return self._read_str(ptr, length) if ptr else ""
        finally:
            self._stack_adjust(16)

    def _call_pointer(self, invoke) -> int:
        """callPointer 模式：结果 ptr@0、错误 obj@4、isError@8。"""
        stack = self._stack_adjust(-16)
        try:
            invoke(stack)
            ptr, obj = self._i32(stack), self._i32(stack, 4)
            if self._i32(stack, 8):
                raise self._obj.take(obj)
            return ptr
        finally:
            self._stack_adjust(16)

    def generate_runtime_auth_fields(self, user_payload: str) -> dict:
        with self._lock:
            raw = self._call_string(lambda stack: self._ex[
                "generate_runtime_auth_fields"](
                self._store, stack, self.write_string(user_payload),
                self._last_len))
            return json.loads(raw)

    def context_new(self, machine_id: str, version: str, user_info: str,
                    metadata: str) -> int:
        with self._lock:
            return self._call_pointer(lambda stack: self._ex[
                "qodercontext_new"](
                self._store, stack,
                self.write_string(machine_id), self._last_len,
                self.write_string(version), self._last_len,
                self.write_string(user_info), self._last_len,
                self.write_string(metadata), self._last_len))

    def prepare_infer_request(self, ctx: int, host: str, body_json: str,
                              model_key: str, source: str) -> dict:
        with self._lock:
            result = self._call_pointer(lambda stack: self._ex[
                "qodercontext_prepareInferRequest"](
                self._store, stack, ctx,
                self.write_string(host), self._last_len,
                self.write_string(body_json), self._last_len,
                self.write_string(model_key), self._last_len,
                self.write_string(source), self._last_len))

            def read_result(export_key: str) -> str:
                stack = self._stack_adjust(-16)
                try:
                    self._ex[export_key](self._store, stack, result)
                    ptr, length = self._i32(stack), self._i32(stack, 4)
                    return self._read_str(ptr, length) if ptr else ""
                finally:
                    self._stack_adjust(16)

            url = read_result("requestresult_url")
            body = read_result("requestresult_body")
            headers = self._obj.take(self._ex["requestresult_headers"](
                self._store, result)) or {}
            return {"url": url, "headers": dict(headers), "body": body}


_INSTANCE: QoderWasm | None = None
_INSTANCE_ERR: str | None = None


def get_wasm() -> QoderWasm:
    """进程级单例；不可用时抛 QoderWasmError（调用方转 501 诚实报错）。"""
    global _INSTANCE, _INSTANCE_ERR
    if _INSTANCE is None:
        if _INSTANCE_ERR:
            raise QoderWasmError(_INSTANCE_ERR)
        try:
            _INSTANCE = QoderWasm()
        except QoderWasmError as exc:
            _INSTANCE_ERR = str(exc)
            raise
    return _INSTANCE
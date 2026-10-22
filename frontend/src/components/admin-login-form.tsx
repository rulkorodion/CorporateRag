"use client";

import { Eye, EyeOff, LoaderCircle, LockKeyhole, LogIn, UserRound } from "lucide-react";
import { useRouter } from "next/navigation";
import { FormEvent, useState } from "react";

import { Button } from "@/components/ui/button";

export function AdminLoginForm() {
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setSubmitting(true);
    try {
      const response = await fetch("/api/admin/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      const body = (await response.json().catch(() => null)) as { message?: string } | null;
      if (!response.ok) throw new Error(body?.message ?? "Не удалось выполнить вход.");
      router.replace("/admin");
      router.refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Не удалось выполнить вход.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={submit} className="w-full max-w-[440px] rounded-[30px] border border-[#dce6ea] bg-white/95 p-7 shadow-[0_18px_55px_rgba(55,86,108,.11)] sm:p-9">
      <span className="grid size-12 place-items-center rounded-[17px] bg-[#edf3f6] text-[#4f7c9c]">
        <LockKeyhole className="size-6" />
      </span>
      <h1 className="mt-5 text-[25px] font-semibold tracking-[-.025em] text-[#294c64]">Вход для редактора</h1>
      <p className="mt-2 text-[14px] leading-6 text-[#738792]">Управление документами доступно только ответственным сотрудникам.</p>

      <label className="mt-7 block text-[13px] font-medium text-[#4b687b]">
        Логин
        <span className="mt-2 flex h-12 items-center gap-3 rounded-[16px] border border-[#dce6ea] bg-[#fbfcfd] px-4 focus-within:border-[#91aebe] focus-within:ring-3 focus-within:ring-[#4f7c9c]/10">
          <UserRound className="size-[18px] text-[#89a0ad]" />
          <input
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            autoComplete="username"
            className="min-w-0 flex-1 bg-transparent text-[14px] text-[#294c64] outline-none placeholder:text-[#a1b0b8]"
            placeholder="Введите логин"
            required
          />
        </span>
      </label>

      <label className="mt-4 block text-[13px] font-medium text-[#4b687b]">
        Пароль
        <span className="mt-2 flex h-12 items-center gap-3 rounded-[16px] border border-[#dce6ea] bg-[#fbfcfd] px-4 focus-within:border-[#91aebe] focus-within:ring-3 focus-within:ring-[#4f7c9c]/10">
          <LockKeyhole className="size-[18px] text-[#89a0ad]" />
          <input
            type={showPassword ? "text" : "password"}
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            autoComplete="current-password"
            className="min-w-0 flex-1 bg-transparent text-[14px] text-[#294c64] outline-none placeholder:text-[#a1b0b8]"
            placeholder="Введите пароль"
            required
          />
          <button type="button" onClick={() => setShowPassword((value) => !value)} className="text-[#8297a3] transition hover:text-[#4f7c9c]" aria-label={showPassword ? "Скрыть пароль" : "Показать пароль"}>
            {showPassword ? <EyeOff className="size-[18px]" /> : <Eye className="size-[18px]" />}
          </button>
        </span>
      </label>

      <div
        className={`grid transition-[grid-template-rows,margin-top,opacity] duration-300 ease-out ${error ? "mt-4 grid-rows-[1fr] opacity-100" : "mt-0 grid-rows-[0fr] opacity-0"}`}
        aria-live="polite"
      >
        <div className="overflow-hidden">
          {error && <p className="rounded-[14px] border border-[#efcfd2] bg-[#fff6f6] px-4 py-3 text-[13px] leading-5 text-[#9a4c55]">{error}</p>}
        </div>
      </div>

      <Button type="submit" className="mt-6 h-12 w-full rounded-[17px]" disabled={submitting}>
        {submitting ? <LoaderCircle className="size-[18px] animate-spin" /> : <LogIn className="size-[18px]" />}
        Войти
      </Button>
    </form>
  );
}

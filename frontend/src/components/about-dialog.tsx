"use client";

import { Info, LockKeyhole, MessagesSquare, ShieldCheck } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";

export function AboutDialog() {
  const [open, setOpen] = useState(false);

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="outline" className="h-[42px] rounded-[25px] px-4 text-[13px] max-sm:size-10 max-sm:p-0">
          <Info className="size-[18px]" />
          <span className="max-sm:sr-only">О базе знаний</span>
        </Button>
      </DialogTrigger>
      <DialogContent className="max-w-[500px] rounded-[32px] p-8 sm:p-10">
        <DialogTitle className="text-[24px] font-semibold tracking-[-.025em]">О базе знаний</DialogTitle>
        <DialogDescription className="mt-3 text-[15px] leading-7 text-[#63789f]">
          Корпоративный помощник ищет информацию во внутренних документах КТЗ и формирует ответ только по найденным источникам.
        </DialogDescription>
        <div className="mt-6 space-y-2.5 text-[14px] leading-6 text-[#53688f]">
          <p className="flex gap-3 rounded-[18px] border border-[#e5eaf3] bg-[#f8faff] p-4.5">
            <ShieldCheck className="mt-0.5 size-5 shrink-0 text-[#4f7c9c]" />
            Каждый ответ сопровождается проверяемыми ссылками на реальные документы.
          </p>
          <p className="flex gap-3 rounded-[18px] border border-[#e5eaf3] bg-[#f8faff] p-4.5">
            <MessagesSquare className="mt-0.5 size-5 shrink-0 text-[#4f7c9c]" />
            Вопросы обрабатываются независимо; история этой страницы хранится только в браузере до обновления.
          </p>
        </div>
        <div className="mt-6 border-t border-[#e5eaf0] pt-5 text-center">
          <Link
            href="/admin/login"
            onClick={() => setOpen(false)}
            className="inline-flex items-center gap-2 rounded-full px-3 py-2 text-[12px] font-medium text-[#718893] transition hover:bg-[#edf3f6] hover:text-[#4f7c9c]"
          >
            <LockKeyhole className="size-4" /> Для редакторов
          </Link>
        </div>
      </DialogContent>
    </Dialog>
  );
}

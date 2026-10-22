"use client";

import Image from "next/image";
import { FormEvent, memo, useRef, useState } from "react";

import { Button } from "@/components/ui/button";

type ChatComposerProps = {
  onSubmit: (question: string) => void;
  disabled?: boolean;
};

export const ChatComposer = memo(function ChatComposer({ onSubmit, disabled = false }: ChatComposerProps) {
  const [question, setQuestion] = useState("");
  const inputRef = useRef<HTMLTextAreaElement>(null);

  function submit(event?: FormEvent) {
    event?.preventDefault();
    const text = question.trim();
    if (!text || disabled) return;

    setQuestion("");
    onSubmit(text);
    requestAnimationFrame(() => inputRef.current?.focus());
  }

  return (
    <div className="composer-shell fixed inset-x-0 bottom-0 z-30 px-5 pb-4 sm:px-8 sm:pb-6">
      <form onSubmit={submit} className="mx-auto flex min-h-[60px] w-full max-w-[1020px] items-end gap-3 rounded-full border border-[#dbe4e8] bg-white py-1.5 pl-5 pr-1.5 shadow-[0_12px_38px_rgba(55,86,108,.12)] sm:items-center sm:pl-6">
        <textarea
          ref={inputRef}
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              submit();
            }
          }}
          rows={1}
          maxLength={4000}
          placeholder="Задайте вопрос по базе знаний..."
          aria-label="Вопрос по базе знаний"
          className="max-h-28 min-h-8 flex-1 resize-none bg-transparent py-1 text-[15px] leading-6 text-[#17366e] outline-none placeholder:text-[#8da0c7] sm:text-[16px]"
        />
        <Button type="submit" size="icon" disabled={disabled || !question.trim()} aria-label="Отправить вопрос" className="size-[46px] shrink-0 overflow-hidden rounded-full bg-transparent p-0 shadow-[0_7px_19px_rgba(79,124,156,.22)] hover:bg-transparent">
          <Image src="/icons/send-accent.png" alt="" width={512} height={512} className="size-full object-contain" />
        </Button>
      </form>
    </div>
  );
});

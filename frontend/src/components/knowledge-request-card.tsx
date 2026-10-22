"use client";

import { CheckCircle2, LoaderCircle, MailPlus } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";

type RequestState = "idle" | "sending" | "sent" | "error";

export function KnowledgeRequestCard({
  question,
  answer,
  requestId,
}: {
  question: string;
  answer: string;
  requestId: string;
}) {
  const [state, setState] = useState<RequestState>("idle");
  const [message, setMessage] = useState("");

  async function sendRequest() {
    if (state === "sending" || state === "sent") return;
    setState("sending");
    setMessage("");
    try {
      const response = await fetch("/api/knowledge-request", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question, answer, requestId }),
      });
      const body = (await response.json().catch(() => null)) as { message?: string } | null;
      if (!response.ok) throw new Error(body?.message ?? "Не удалось отправить заявку.");
      setState("sent");
      setMessage(body?.message ?? "Заявка отправлена.");
    } catch (error) {
      setState("error");
      setMessage(error instanceof Error ? error.message : "Не удалось отправить заявку.");
    }
  }

  return (
    <section className="mt-6 rounded-[22px] border border-[#dce6ea] bg-[#f7fafb] p-5 sm:p-6">
      <div className="flex items-start gap-3.5">
        <span className="grid size-10 shrink-0 place-items-center rounded-[15px] bg-[#eaf1f4] text-[#4f7c9c]">
          <MailPlus className="size-5" />
        </span>
        <div className="min-w-0 flex-1">
          <h3 className="text-[14px] font-semibold text-[#36586d]">Не нашли нужную информацию?</h3>
          <p className="mt-1 text-[12px] leading-5 text-[#768b96]">
            Отправьте заявку на добавление документа по этому вопросу. Ответственные сотрудники смогут дополнить базу знаний.
          </p>
        </div>
      </div>

      <Button
        type="button"
        variant={state === "sent" ? "outline" : "default"}
        className="mt-4 h-10 rounded-[16px] px-4 text-[12px]"
        onClick={sendRequest}
        disabled={state === "sending" || state === "sent"}
      >
        {state === "sending" ? (
          <LoaderCircle className="size-4 animate-spin" />
        ) : state === "sent" ? (
          <CheckCircle2 className="size-4" />
        ) : (
          <MailPlus className="size-4" />
        )}
        {state === "sending" ? "Отправляем…" : state === "sent" ? "Заявка отправлена" : "Отправить заявку"}
      </Button>

      <div
        className={`grid transition-[grid-template-rows,margin-top,opacity] duration-300 ease-out ${message ? "mt-3 grid-rows-[1fr] opacity-100" : "mt-0 grid-rows-[0fr] opacity-0"}`}
        aria-live="polite"
      >
        <div className="overflow-hidden">
          {message && (
            <p className={`text-[11px] leading-5 ${state === "error" ? "text-[#a2575e]" : "text-[#527461]"}`}>
              {message}
            </p>
          )}
        </div>
      </div>
    </section>
  );
}

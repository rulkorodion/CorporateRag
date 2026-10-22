import { X } from "lucide-react";

import { Button } from "@/components/ui/button";

export function LoadingAnswer({
  status = "Ищу ответ в документах",
  onCancel,
}: {
  status?: string;
  onCancel?: () => void;
}) {
  return (
    <div className="answer-card flex min-h-32 items-center justify-between gap-4" role="status" aria-label={status}>
      <div className="flex min-w-0 items-center gap-3">
        <span className="text-[15px] text-[#63789e]">{status}</span>
        <span className="flex shrink-0 gap-1.5">
          {[0, 1, 2].map((index) => (
            <span key={index} className="size-2 animate-bounce rounded-full bg-[#4f7c9c]" style={{ animationDelay: `${index * 130}ms` }} />
          ))}
        </span>
      </div>
      {onCancel && (
        <Button type="button" variant="ghost" size="sm" onClick={onCancel} className="shrink-0 rounded-full text-[#748995]">
          <X className="size-4" /> Отменить
        </Button>
      )}
    </div>
  );
}

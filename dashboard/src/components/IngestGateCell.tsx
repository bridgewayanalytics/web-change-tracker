"use client";

import React, { useState } from "react";

interface IngestGateCellProps {
  status: string | null | undefined;
  onPublish: () => void;
}

export function IngestGateCell({ status, onPublish }: IngestGateCellProps) {
  const [localStatus] = useState<string | null | undefined>(status);
  const effective = status ?? localStatus;

  if (!effective || effective === "rejected") {
    return (
      <button
        onClick={onPublish}
        className="text-[11px] font-medium text-blue-600 hover:text-blue-800 hover:underline"
      >
        Publish
      </button>
    );
  }

  if (effective === "approved") {
    return <span className="text-[11px] text-green-600 font-medium">✓ Published</span>;
  }

  // pending
  return (
    <button
      onClick={onPublish}
      className="text-[11px] font-medium text-blue-600 hover:text-blue-800 hover:underline"
    >
      Publish
    </button>
  );
}

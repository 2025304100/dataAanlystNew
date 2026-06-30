import { useState, useEffect } from "react";
import { onRequestChange } from "../api/client";

export function useActiveRequests() {
  const [count, setCount] = useState(0);
  useEffect(() => {
    return onRequestChange(setCount);
  }, []);
  return count;
}

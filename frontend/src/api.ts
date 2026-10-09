export class ApiError extends Error {
  constructor(message: string, public status: number) { super(message); }
}

export async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, options);
  const body = await response.json();
  if (!response.ok) {
    throw new ApiError(typeof body.detail === 'string' ? body.detail : 'Request could not finish. Try again.', response.status);
  }
  return body as T;
}

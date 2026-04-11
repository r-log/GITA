import axios from 'axios';

const handleClick = (e: MouseEvent): void => {
  console.log(e);
};

const square = (x: number): number => x * x;

const asyncFetch = async (url: string): Promise<Response> => {
  return fetch(url);
};

const parseConfig = (raw: string): object | null => {
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
};

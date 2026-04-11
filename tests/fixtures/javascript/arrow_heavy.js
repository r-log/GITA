import axios from 'axios';

const handleClick = (e) => {
  console.log(e);
};

const square = (x) => x * x;

const asyncFetch = async (url) => {
  return fetch(url);
};

const parseConfig = (raw) => {
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
};

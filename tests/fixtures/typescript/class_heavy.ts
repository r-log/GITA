import { Base } from './base';

interface Animal {
  name: string;
  sound(): string;
}

class Dog implements Animal {
  name: string;

  constructor(name: string) {
    this.name = name;
  }

  sound(): string {
    return 'Woof';
  }

  async fetch(): Promise<void> {
    return;
  }
}

class Cat implements Animal {
  constructor(public name: string) {}

  sound(): string {
    return 'Meow';
  }
}

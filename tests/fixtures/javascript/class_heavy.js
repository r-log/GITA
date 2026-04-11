import { Base } from './base';

class Dog extends Base {
  constructor(name) {
    super();
    this.name = name;
  }

  sound() {
    return 'Woof';
  }

  async fetch() {
    return;
  }
}

class Cat {
  constructor(name) {
    this.name = name;
  }

  sound() {
    return 'Meow';
  }
}

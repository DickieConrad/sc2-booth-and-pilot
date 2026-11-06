import { ApplicationConfig, ErrorHandler } from '@angular/core';
import { PageErrorHandler } from './app';

// Zoneless (Angular 21 default). No global rejection listener: like the old page, only real errors hit the chip.
export const appConfig: ApplicationConfig = {
  providers: [{ provide: ErrorHandler, useClass: PageErrorHandler }],
};
